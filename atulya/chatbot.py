"""Grounded orchestration for the Atulya Yatra travel assistant.

The assistant intentionally does not invent tourism facts. It parses a small set
of travel intents, resolves names through :class:`SearchService`, and builds
recommendations/itineraries only from the application's curated place records.
Conversation state is supplied by the API and persisted in SQLite, keeping this
service deterministic and safe to use with multiple Gunicorn workers.
"""
from __future__ import annotations

import copy
import re
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from .data_store import DataStore
from .geo import distance_between
from .recommend import rank_hidden_gems, recommend_for_preferences
from .search import SearchService

MODES = ("ask", "discover", "plan", "hidden")

EXPERIENCE: Dict[str, List[str]] = {
    "Adventure": ["adventure"],
    "Peace": ["peace"],
    "Nature": ["nature"],
    "Detox": ["detox", "wellness"],
    "Culture": ["culture", "heritage"],
    "Spiritual": ["spiritual"],
}

FOLLOW_UP: Dict[str, Dict[str, List[str]]] = {
    "Adventure": {
        "Trekking": ["trekking", "adventure", "mountains"],
        "Camping": ["camping", "adventure", "nature"],
        "Rafting": ["rafting", "adventure", "rivers"],
        "Bike Ride": ["adventure", "mountains"],
    },
    "Peace": {
        "Mountains": ["mountains", "peace"],
        "Lakes": ["lakes", "peace"],
        "Quiet Villages": ["peace", "nature"],
        "Backwaters": ["backwaters", "peace"],
    },
    "Nature": {
        "Mountains": ["mountains", "nature"],
        "Waterfalls": ["waterfalls", "nature"],
        "Forests": ["nature", "wildlife"],
        "Wildlife": ["wildlife", "nature"],
    },
    "Detox": {
        "Wellness": ["detox", "peace"],
        "Yoga": ["spiritual", "detox"],
        "Nature Escape": ["nature", "detox"],
        "Beaches": ["beaches", "detox"],
    },
    "Culture": {
        "Heritage": ["history", "culture", "heritage"],
        "Architecture": ["architecture", "heritage"],
        "Temples": ["temples", "spiritual"],
        "Festivals": ["culture"],
    },
    "Spiritual": {
        "Temples": ["temples", "spiritual"],
        "Monasteries": ["spiritual", "monasteries", "mountains"],
        "Sacred Rivers": ["rivers", "spiritual"],
        "Pilgrimage": ["spiritual", "temples"],
    },
}

SCOPES = {
    "Popular": "popular",
    "Hidden Gems": "hidden",
    "Both": "both",
}
PLAN_STYLES = ["Balanced", "Nature", "Adventure", "Culture", "Relaxed", "Spiritual"]
BUDGETS = ["Budget", "Moderate", "Comfort", "Skip"]
TRAVELLERS = ["Solo", "Couple", "Family", "Friends", "With parents"]

# Every label the planner ever offers as a tappable chip. A chip answers the
# question we just asked; it is never the name of a place. Without this, tapping
# "Skip" on the details step was geocoded and overwrote the chosen destination,
# so a Spiti Valley trip came back as a plan for "SKIP-India".
_PLAN_CHIP_LABELS = frozenset(
    label.lower()
    for label in (
        *PLAN_STYLES, *BUDGETS, *TRAVELLERS,
        "Hidden Gems", "Popular", "Skip",
        "1 day", *(f"{n} days" for n in range(2, 15)),
    )
)

_MOOD_SYNONYMS = {
    "adventurous": "Adventure", "adventure": "Adventure",
    "peaceful": "Peace", "peace": "Peace", "quiet": "Peace",
    "nature": "Nature", "natural": "Nature",
    "detox": "Detox", "wellness": "Detox", "yoga": "Detox",
    "culture": "Culture", "cultural": "Culture", "heritage": "Culture", "history": "Culture",
    "spiritual": "Spiritual", "pilgrimage": "Spiritual", "religious": "Spiritual",
}
_STYLE_SYNONYMS = {
    "balanced": "Balanced", "nature": "Nature", "adventure": "Adventure",
    "adventurous": "Adventure", "culture": "Culture", "cultural": "Culture",
    "heritage": "Culture", "relaxed": "Relaxed", "relaxing": "Relaxed",
    "slow": "Relaxed", "spiritual": "Spiritual", "pilgrimage": "Spiritual",
}


def _normalise(text: object) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip())


def _contains(text: str, phrase: str) -> bool:
    return bool(re.search(rf"\b{re.escape(phrase.lower())}\b", text.lower()))


# Anchored, so it only fires when the *whole* message is a pleasantry. "hi" alone
# is small talk; "hi, plan 3 days in Goa" is a planning request.
_SMALL_TALK_RE = re.compile(
    r"^\s*(?:"
    r"hi+|hey+|hello+|helo+|yo|sup|wassup|namaste|namaskar|hola|salaam|"
    r"good\s*(?:morning|afternoon|evening|day|night)|"
    r"how\s+(?:are|r)\s+(?:you|u)|how'?s\s+it\s+going|what'?s\s+up|"
    r"who\s+are\s+you|what\s+(?:can|do)\s+you\s+do|what\s+is\s+this|"
    r"help|start|menu|hi\s+there|hey\s+there|"
    r"thanks?|thank\s+you|thx|ty|"
    r"ok|okay|k|cool|nice|great|awesome|good|"
    r"bye|goodbye|see\s+you|cya"
    r")\s*[!.?]*\s*$",
    re.IGNORECASE,
)

# Below this a fragment starts prefix-matching real names ("hi" scores 85
# against "Himachal Pradesh"), so the search index is not consulted at all.
_MIN_LOCATION_QUERY = 3
# Short queries must match much more strongly than long ones before we accept
# them as a place name.
_SHORT_QUERY_SCORE = 88
_NORMAL_QUERY_SCORE = 70


def _is_small_talk(text: str) -> bool:
    return bool(_SMALL_TALK_RE.match(str(text or "")))


def _option(label: str, value: Optional[str] = None, action: str = "answer") -> dict:
    return {"label": label, "value": value if value is not None else label, "action": action}


class ChatAssistant:
    """Deterministic assistant that coordinates existing trusted services."""

    def __init__(
        self,
        store: DataStore,
        search: Optional[SearchService] = None,
        place_resolver: Optional[Callable[[str], Optional[dict]]] = None,
        nearby_search: Optional[Callable[..., List[dict]]] = None,
        nearby_grouper: Optional[Callable[[List[dict]], dict]] = None,
        llm=None,
    ):
        self.store = store
        self.search = search or SearchService(store)
        self.place_resolver = place_resolver
        self.nearby_search = nearby_search
        self.nearby_grouper = nearby_grouper
        # Optional. Used only to word replies about facts already retrieved from
        # our own data; see atulya/llm.py. None keeps the assistant fully
        # deterministic.
        self.llm = llm

    # Rules the model is held to on every call. The facts are supplied per turn;
    # anything outside them is off limits.
    _GROUNDING_RULES = (
        "You are the travel assistant for Atulya Yatra, a guide to travelling in India. "
        "Answer ONLY using the FACTS block supplied with the question. "
        "Never invent or guess place names, prices, opening hours, distances, dates or "
        "festival timings. "
        "Only admit to missing information when the visitor actually asked for something "
        "the FACTS do not cover. If their question is answered, do not mention missing "
        "details at all — an unprompted disclaimer reads as evasive. "
        "Facts marked as describing a state or region as a whole must be presented that "
        "way — as something found across the region — never as a speciality of the "
        "individual place. "
        "Never refer to the FACTS, 'our data', 'our guides', a database or any internal "
        "source. Simply state what is true. "
        "Do not greet the visitor or introduce yourself unless their own message is a "
        "greeting. "
        "Do not use markdown, headings, bullet points or emoji. "
        "Write 2-4 short sentences in warm, plain British English. "
        "Do not list places the visitor can already see on screen; the page shows them."
    )

    def _narrate(self, facts: str, question: str, fallback: str) -> str:
        """Let the model word a reply about facts we already hold.

        Returns ``fallback`` untouched whenever the model is unavailable, slow or
        unhelpful, so behaviour never depends on the network.
        """
        if not self.llm or not getattr(self.llm, "enabled", False):
            return fallback
        try:
            reply = self.llm.complete(
                system=self._GROUNDING_RULES,
                user=f"FACTS:\n{facts}\n\nVISITOR ASKED: {question or 'Tell me about this place.'}",
            )
        except Exception:  # noqa: BLE001
            # An optional nicety must never break a conversation turn, whatever
            # the provider does — including errors its own client misses.
            return fallback
        return reply or fallback

    @staticmethod
    def initial_state() -> dict:
        return {
            "mode": None,
            "stage": "welcome",
            "preferences": {"excluded": []},
            "destination": None,
            "shown": 0,
            "itinerary": None,
            "revision": 0,
        }

    def respond(
        self,
        state: Optional[dict],
        *,
        message: str = "",
        mode: Optional[str] = None,
        action: Optional[dict] = None,
        context: Optional[dict] = None,
    ) -> Tuple[dict, dict]:
        """Process one turn and return ``(response, state)``.

        State is copied before use so a malformed request cannot mutate a shared
        object. Every destination/context slug is revalidated against DataStore.
        """
        current = copy.deepcopy(state) if isinstance(state, dict) else self.initial_state()
        current.setdefault("preferences", {}).setdefault("excluded", [])
        current.setdefault("revision", 0)
        message = _normalise(message)[:1000]
        action = action if isinstance(action, dict) else {}

        if self._is_reset(message, action):
            current = self.initial_state()
            return self._welcome(), current

        # A greeting is not a travel query. Without this, "hi" fell through to
        # location resolution and prefix-matched a state, so saying hello
        # produced an itinerary for Himachal Pradesh.
        if message and not action and _is_small_talk(message):
            return self._small_talk(message, current), current

        validated_context = self._validate_context(context)
        if validated_context:
            current["context"] = validated_context
            if validated_context["type"] in {"destination", "hidden_gem", "state"}:
                current["destination"] = validated_context

        requested_mode = action.get("value") if action.get("type") == "set_mode" else mode
        if requested_mode in MODES and (requested_mode != current.get("mode") or action.get("type") == "set_mode"):
            current = self._start_mode(current, requested_mode)

        if action.get("type") == "plan_place":
            place = self.store.get_place(str(action.get("value") or ""))
            if place:
                current = self._start_mode(current, "plan")
                current["destination"] = self._location_ref(place)
                current["stage"] = "duration"
                return self._plan_next(current), current

        if action.get("type") == "show_more" and current.get("mode") == "discover":
            return self._discovery_results(current, more=True), current

        if current.get("mode") == "plan" and current.get("itinerary") and not message and not action:
            return self._existing_plan_response(current), current

        if current.get("itinerary") and self._looks_like_modification(message, action):
            return self._modify_itinerary(current, message or str(action.get("value") or "")), current

        if not current.get("mode"):
            if not message and not action:
                return self._welcome(), current
            inferred = self._infer_mode(message)
            # One input drives the experience. Planning language keeps the
            # itinerary workflow; every other query starts with grounded search.
            current = self._start_mode(current, "plan" if inferred == "plan" else "ask")

        answer = str(action.get("value") or message or "") if action.get("type") == "answer" else message
        if current["mode"] == "ask":
            response = self._ask(current, answer)
        elif current["mode"] == "discover":
            response = self._discover(current, answer)
        elif current["mode"] == "hidden":
            response = self._hidden(current, answer)
        else:
            response = self._plan(current, answer)
        return response, current

    def _welcome(self) -> dict:
        return self._response(
            mode=None,
            stage="welcome",
            message=(
                "Hello — I can plan a trip day by day, or answer questions about "
                "food, festivals, culture and the best time to visit anywhere in India.\n"
                "What would you like to do?"
            ),
            options=[
                _option("Plan a trip", "plan", "set_mode"),
                _option("Find hidden gems", "hidden", "set_mode"),
            ],
            suggestions=[
                "Plan 3 days in Udaipur",
                "What food should I try in Vadodara?",
                "Best time to visit Ladakh",
            ],
        )

    # Openers that deserve a reply rather than a search. Kept short and led by
    # what the visitor can do next.
    _SMALL_TALK_OPENERS = {
        "thanks": "You're welcome. Anything else you'd like to look into?",
        "bye": "Safe travels. Come back whenever you want to plan the next one.",
    }

    def _small_talk(self, message: str, state: dict) -> dict:
        """Reply to a greeting without treating it as a destination."""
        lower = message.lower().strip(" !.?")
        if lower.startswith(("thank", "thx", "ty")):
            base = self._SMALL_TALK_OPENERS["thanks"]
        elif lower.startswith(("bye", "goodbye", "see you", "cya")):
            base = self._SMALL_TALK_OPENERS["bye"]
        elif "who are you" in lower or "what can you do" in lower or "what is this" in lower:
            base = (
                "I'm the Atulya Yatra travel assistant. I can build a day-by-day "
                "itinerary for anywhere in India, or answer questions about food, "
                "festivals, culture, packing and the best time to visit — all from "
                "our own destination guides."
            )
        else:
            base = (
                "Hello. Tell me where you're headed and how long you have, and I'll "
                "lay out a day-by-day plan. You can also just ask me about a place."
            )

        # Small talk carries no facts, so the model is free to phrase the
        # greeting naturally; it still cannot introduce travel claims because the
        # FACTS block says so.
        message_text = self._narrate(
            "This is a greeting or small talk. No travel facts are involved. "
            "Capabilities: planning day-by-day India itineraries, and answering "
            "questions about food, festivals, culture, packing and best time to visit, "
            "using Atulya Yatra's own destination guides.",
            message,
            base,
        )

        return self._response(
            mode=state.get("mode"),
            stage="welcome",
            message=message_text,
            options=[
                _option("Plan a trip", "plan", "set_mode"),
                _option("Find hidden gems", "hidden", "set_mode"),
            ],
            suggestions=[
                "Plan 3 days in Udaipur",
                "What food should I try in Vadodara?",
                "Best time to visit Ladakh",
            ],
        )

    def _start_mode(self, state: dict, mode: str) -> dict:
        context = state.get("context")
        destination = state.get("destination") if context else None
        fresh = self.initial_state()
        fresh["mode"] = mode
        fresh["destination"] = destination
        fresh["context"] = context
        if mode == "ask":
            fresh["stage"] = "question"
        elif mode == "discover":
            fresh["stage"] = "experience"
        elif mode == "hidden":
            fresh["stage"] = "location"
        else:
            fresh["stage"] = "duration" if destination else "location"
        return fresh

    @staticmethod
    def _is_reset(message: str, action: dict) -> bool:
        return action.get("type") == "reset" or message.lower() in {
            "reset", "clear", "start over", "clear chat", "new trip"
        }

    @staticmethod
    def _infer_mode(message: str) -> Optional[str]:
        lower = message.lower()
        if not lower:
            return None
        if "hidden gem" in lower or "offbeat" in lower:
            return "hidden"
        if "itinerary" in lower or "plan" in lower or re.search(r"\b\d+\s*days?\b", lower):
            return "plan"
        if "?" in message or re.match(r"^(what|when|where|which|how|is|are|can|should|tell me)\b", lower):
            return "ask"
        return "discover"

    # ----------------------------------------------------- grounded answers
    def _ask(self, state: dict, text: str) -> dict:
        if text:
            location = self._resolve_location(text)
            if location:
                state["destination"] = location
        location = state.get("destination")
        if not text and location:
            return self._location_experience(state, location, "")
        if not location:
            state["stage"] = "question"
            interests = self._extract_interest_words(text)
            if interests:
                ranked = recommend_for_preferences(
                    self.store.all_places(), interests, prefer="both", limit=6
                )
                cards = [
                    self._place_card(place, f"Matches {', '.join(interests[:3])}")
                    for place in ranked
                ]
                return self._response(
                    mode="ask", stage="answer",
                    message="I could not identify one location, so these are the closest matches in Atulya Yatra's curated data.",
                    cards=cards,
                    suggestions=["Explore Udaipur", "Places near Majuli", "Plan 3 days in Kochi"],
                )
            return self._response(
                mode="ask", stage="question",
                message="I could not identify that Indian location. Try its city, district, state, or a nearby landmark.",
                suggestions=["Explore Udaipur", "Places near Majuli", "Plan 3 days in Kochi"],
            )
        return self._location_experience(state, location, text)

    def _location_experience(self, state: dict, location: dict, question: str) -> dict:
        """Build one visual response from live Places and curated state facts."""
        record = self._record_for_location(location)
        origin = dict(record or location)
        origin.setdefault("name", location.get("name"))
        origin.setdefault("slug", location.get("slug"))
        origin.setdefault("state", location.get("state"))
        origin.setdefault("state_slug", location.get("state_slug"))
        origin.setdefault("lat", location.get("lat"))
        origin.setdefault("lng", location.get("lng"))
        origin.setdefault("google_photo_names", location.get("google_photo_names") or [])
        origin.setdefault("google_maps_url", location.get("google_maps_url"))
        origin.setdefault("address", location.get("address"))
        if location.get("type") == "state":
            origin["state"] = origin.get("name")
            origin["state_slug"] = origin.get("slug")
        origin["candidate_type"] = "Location"

        state_slug = origin.get("slug") if location.get("type") == "state" else origin.get("state_slug")
        guide = self.store.state_guide(state_slug) if state_slug else None
        if not guide:
            guide = self._general_guide()

        live_places: List[dict] = []
        if self.nearby_search and origin.get("lat") is not None and origin.get("lng") is not None:
            live_places = self.nearby_search(origin, radius_km=40, max_results=20)
            origin_id = origin.get("google_place_id")
            origin_name = str(origin.get("name") or "").lower()
            live_places = [
                place for place in live_places
                if (not origin_id or place.get("google_place_id") != origin_id)
                and str(place.get("name") or "").lower() != origin_name
            ]
        groups = self.nearby_grouper(live_places) if self.nearby_grouper else {
            "top": live_places[:6], "nearby": live_places[6:12], "offbeat": live_places[12:16]
        }

        if location.get("type") == "state":
            curated_destinations = [dict(place) for place in self.store.destinations_in_state(state_slug)]
            curated_hidden = [dict(place) for place in self.store.hidden_gems_in_state(state_slug)]
        else:
            curated_nearby = self.store.within_radius(origin, 120, exclude_slug=origin.get("slug"))
            curated_destinations = [place for place in curated_nearby if not place.get("is_hidden_gem")]
            curated_hidden = [place for place in curated_nearby if place.get("is_hidden_gem")]

        if not groups.get("top"):
            groups["top"] = sorted(
                curated_destinations,
                key=lambda place: (float(place.get("rating") or 0), int(place.get("reviews") or 0)),
                reverse=True,
            )[:6]
            for place in groups["top"]:
                place["candidate_type"] = "Featured destination"

        sections = []
        if groups.get("top"):
            sections.append({
                "key": "top_attractions",
                "title": "Top attractions",
                "subtitle": "Popular places from live Google Places results.",
                "cards": [self._place_card(place) for place in groups["top"]],
            })
        if groups.get("nearby"):
            sections.append({
                "key": "nearby_attractions",
                "title": "Nearby attractions",
                "subtitle": "More places within about 40 km, ordered for local discovery.",
                "cards": [self._place_card(place) for place in groups["nearby"]],
            })
        if curated_hidden:
            curated_hidden = sorted(
                curated_hidden,
                key=lambda place: float(place.get("distance_km") or 9999),
            )[:4]
            for place in curated_hidden:
                place["candidate_type"] = "Curated hidden gem"
            sections.append({
                "key": "curated_hidden_gems",
                "title": "Curated hidden gems",
                "subtitle": "Verified Atulya Yatra records near this location or within the state.",
                "cards": [self._place_card(place) for place in curated_hidden],
            })
        if groups.get("offbeat"):
            sections.append({
                "key": "offbeat_candidates",
                "title": "Offbeat candidates",
                "subtitle": "Lower-review live Places candidates to research before visiting; these are not curated hidden gems.",
                "cards": [self._place_card(place) for place in groups["offbeat"]],
            })

        specific = bool(re.search(
            r"\b(food|eat|cuisine|festival|language|culture|tradition|safe|safety|pack|packing|best time|when)\b",
            question.lower(),
        ))
        if specific and record:
            message = self._grounded_answer(record, question)
        elif record and record.get("short_intro"):
            message = record["short_intro"]
        elif record and record.get("intro"):
            message = record["intro"]
        else:
            message = f"Explore {origin.get('name')} with live attractions and practical state-level guidance below."

        # Word the answer conversationally, strictly from what we just gathered.
        message = self._narrate(
            self._fact_sheet(origin, record, guide, message), question, message
        )

        if not live_places:
            message += " Live nearby results are unavailable right now, so available curated places are shown instead."

        state["stage"] = "answer"
        return self._response(
            mode="ask", stage="answer", message=message,
            location=self._place_card(origin), sections=sections, guide=guide,
            suggestions=[
                f"Plan 3 days in {origin.get('name')}",
                f"What food should I try in {origin.get('name')}?",
                f"What should I pack for {origin.get('name')}?",
            ],
            disclaimer="Live attraction details can change. Confirm timings, access, weather, permits and transport before travel.",
        )

    @staticmethod
    def _fact_sheet(
        origin: dict, record: Optional[dict], guide: Optional[dict], grounded: str
    ) -> str:
        """Everything the model is allowed to use for this answer, and nothing else."""
        record = record or {}
        guide = guide or {}
        name = origin.get("name") or "this place"
        state = origin.get("state") or ""

        # Place-level and state-level facts are kept in separate, explicitly
        # labelled blocks. Flattened together they get conflated: Assam's tea
        # heritage came back described as a speciality of Majuli island.
        place = [f"Place: {name}"]
        if state:
            place.append(f"State it sits in: {state}")
        if record.get("type"):
            place.append(f"Type: {record['type']}")
        if record.get("short_intro") or record.get("intro"):
            place.append(f"About {name}: {record.get('short_intro') or record.get('intro')}")
        if record.get("rating"):
            place.append(f"Rating: {record['rating']}")
        if record.get("activities"):
            place.append(f"Things to do at {name}: {', '.join(map(str, record['activities'][:6]))}")
        attractions = [
            a.get("name") for a in (record.get("attractions") or []) if isinstance(a, dict) and a.get("name")
        ]
        if attractions:
            place.append(f"Attractions on the page: {', '.join(map(str, attractions[:6]))}")
        place.append(f"Our own factual summary of {name}: {grounded}")

        region = state or "this region"
        wide = []
        if guide.get("best_time"):
            wide.append(f"Best time to visit {region}: {guide['best_time']}")
        if guide.get("languages"):
            wide.append(f"Languages spoken across {region}: {', '.join(map(str, guide['languages'][:4]))}")
        if guide.get("food"):
            wide.append(f"Dishes found across {region}: {', '.join(map(str, guide['food'][:6]))}")
        festivals = [
            f.get("name") for f in (guide.get("festivals") or []) if isinstance(f, dict) and f.get("name")
        ]
        if festivals:
            wide.append(f"Festivals celebrated across {region}: {', '.join(map(str, festivals[:5]))}")
        if guide.get("culture"):
            wide.append(f"Culture of {region}: {str(guide['culture'])[:500]}")
        if guide.get("travel_tips"):
            wide.append(f"Travel tips for {region}: {' | '.join(map(str, guide['travel_tips'][:3]))}")

        if not wide:
            return "\n".join(place)
        return "\n".join(
            place
            + [
                "",
                f"The following describe {region} as a whole, NOT {name} specifically. "
                f"Treat them as regional context only:",
            ]
            + wide
        )

    @staticmethod
    def _general_guide() -> dict:
        return {
            "state_name": "General India guidance",
            "intro": "State-specific cultural details are unavailable for this result.",
            "languages": [], "best_time": "Check the local forecast and seasonal access for your dates.",
            "culture": "Respect local customs, dress requirements and photography rules, especially at religious places.",
            "food": [], "festivals": [], "travel_tips": [], "activities": [],
            "wildlife": [], "spiritual_places": [],
            "safety_tips": [
                "Check official weather, road and local travel advisories.",
                "Keep identification and emergency contacts accessible and use registered transport.",
                "Confirm opening hours, permits and access rules directly before visiting.",
            ],
            "packing": [
                "Government ID and digital copies of bookings",
                "Weather-appropriate layers and comfortable walking shoes",
                "Water bottle, prescribed medicines and a compact first-aid kit",
                "Sun or rain protection and a modest cover for religious sites",
            ],
            "guidance_note": "Safety and packing are general guidance, not location-specific advice.",
        }

    def _record_for_location(self, location: dict) -> Optional[dict]:
        if location.get("type") == "state":
            state = self.store.get_state(location.get("slug"))
            if state:
                return {**state, "kind": "state"}
            return None
        return self.store.get_place(location.get("slug"))

    def _grounded_answer(self, record: dict, question: str) -> str:
        lower = question.lower()
        name = record.get("name", "This place")
        if any(word in lower for word in ("food", "eat", "dish", "cuisine")):
            foods = record.get("food") or []
            values = [self._named_item(item) for item in foods]
            values = [value for value in values if value]
            return f"Food recorded for {name}: {'; '.join(values[:5])}." if values else f"We do not yet have verified food details for {name}."
        if any(word in lower for word in ("when", "best time", "season", "month", "weather")):
            timing = record.get("best_time_to_visit") or record.get("best_time")
            if not timing and record.get("best_season"):
                timing = ", ".join(record["best_season"])
            return f"Best-time guidance in our data for {name}: {timing}" if timing else f"We do not yet have verified seasonal guidance for {name}."
        if any(word in lower for word in ("safe", "safety", "family", "parents", "children")):
            safety = record.get("safety") or {}
            tips = safety.get("tips") if isinstance(safety, dict) else []
            tips = tips or record.get("tips") or []
            if tips:
                return f"For {name}, our stored guidance says: {' '.join(str(t) for t in tips[:4])} Check current official advice before travelling."
            return f"We do not have enough destination-specific safety data to judge {name}. Check current official advice before travelling."
        if any(word in lower for word in ("attraction", "see", "visit", "do", "places")):
            attractions = record.get("attractions") or []
            values = [self._named_item(item) for item in attractions]
            values = [value for value in values if value]
            if not values and record.get("kind") == "state":
                values = [p.get("name") for p in self.store.destinations_in_state(record.get("slug"))]
            return f"Verified suggestions for {name}: {'; '.join(values[:6])}." if values else f"We do not yet have verified attraction details for {name}."
        if any(word in lower for word in ("culture", "tradition", "local", "etiquette")):
            culture = record.get("culture") or {}
            summary = culture.get("summary") if isinstance(culture, dict) else str(culture)
            return f"Our cultural note for {name}: {summary}" if summary else f"We do not yet have a verified cultural note for {name}."
        if any(word in lower for word in ("festival", "celebration")):
            festivals = [self._named_item(item) for item in (record.get("festivals") or [])]
            festivals = [value for value in festivals if value]
            return f"Festivals recorded for {name}: {'; '.join(festivals[:5])}." if festivals else f"We do not yet have verified festival details for {name}."
        if any(word in lower for word in ("reach", "transport", "airport", "rail", "train", "bus", "travel")):
            info = record.get("travel_info") or {}
            parts = [f"{key.replace('_', ' ').title()}: {value}" for key, value in info.items() if value and key in {"airport", "railway", "bus", "local_transport"}]
            return f"Stored transport guidance for {name}: {' '.join(parts)}" if parts else f"We do not yet have verified transport details for {name}."
        overview = record.get("overview") or record.get("short_intro") or record.get("intro")
        return str(overview) if overview else f"We do not yet have enough verified detail to answer that question about {name}."

    def _answer_suggestions(self, record: dict, question: str) -> List[dict]:
        if record.get("kind") == "state":
            pool = self.store.destinations_in_state(record.get("slug"))
            ranked = recommend_for_preferences(pool, self._extract_interest_words(question), prefer="both", limit=3)
            return [self._place_card(p, f"Explore in {record.get('name')}") for p in ranked]
        cards = [self._place_card(record, "The place your answer is based on")]
        pool = [p for p in self.store.places_in_state(record.get("state_slug")) if p.get("slug") != record.get("slug")]
        ranked = recommend_for_preferences(pool, record.get("categories") or [], prefer="both", limit=2)
        cards.extend(self._place_card(p, f"Related place in {record.get('state')}") for p in ranked)
        return cards

    @staticmethod
    def _named_item(item: object) -> str:
        if isinstance(item, dict):
            name = item.get("name")
            description = item.get("description")
            return f"{name} — {description}" if name and description else str(name or "")
        return str(item or "")

    # ---------------------------------------------------------- discovery
    def _discover(self, state: dict, text: str) -> dict:
        prefs = state["preferences"]
        self._parse_discovery_preferences(text, prefs, state.get("stage"))
        return self._discovery_next(state)

    def _parse_discovery_preferences(self, text: str, prefs: dict, stage: Optional[str]) -> None:
        if not text:
            return
        lower = text.lower()
        exact = text.strip().lower()

        for mood in EXPERIENCE:
            if exact == mood.lower() or _contains(lower, mood):
                prefs["experience"] = mood
                break
        if not prefs.get("experience"):
            for word, mood in _MOOD_SYNONYMS.items():
                if _contains(lower, word):
                    prefs["experience"] = mood
                    break

        experience = prefs.get("experience")
        follow_options = FOLLOW_UP.get(experience, {})
        for focus in follow_options:
            if exact == focus.lower() or _contains(lower, focus):
                prefs["focus"] = focus
                break
        # A few natural variants used in free text.
        focus_aliases = {
            "mountain": "Mountains", "lake": "Lakes", "forest": "Forests",
            "waterfall": "Waterfalls", "monastery": "Monasteries",
            "history": "Heritage", "historical": "Heritage", "trek": "Trekking",
        }
        if not prefs.get("focus"):
            for alias, focus in focus_aliases.items():
                if focus in follow_options and _contains(lower, alias):
                    prefs["focus"] = focus
                    break

        if "both" in lower or "mix" in lower:
            prefs["scope"] = "both"
        elif "hidden" in lower or "offbeat" in lower:
            prefs["scope"] = "hidden"
        elif "popular" in lower or "famous" in lower:
            prefs["scope"] = "popular"
        elif stage == "scope":
            for label, value in SCOPES.items():
                if exact == label.lower():
                    prefs["scope"] = value

    def _discovery_next(self, state: dict) -> dict:
        prefs = state["preferences"]
        experience = prefs.get("experience")
        if not experience:
            state["stage"] = "experience"
            return self._response(
                mode="discover", stage="experience",
                message="What kind of experience are you in the mood for?",
                options=[_option(x) for x in EXPERIENCE],
                suggestions=["Nature and mountains", "Culture and heritage", "A peaceful escape"],
            )
        if not prefs.get("focus"):
            state["stage"] = "focus"
            return self._response(
                mode="discover", stage="focus",
                message=f"Within {experience.lower()}, what draws you most?",
                options=[_option(x) for x in FOLLOW_UP[experience]],
            )
        if not prefs.get("scope"):
            state["stage"] = "scope"
            return self._response(
                mode="discover", stage="scope",
                message="Should I prioritise popular places, genuine hidden gems, or a mix?",
                options=[_option(label) for label in SCOPES],
            )
        return self._discovery_results(state)

    def _interests(self, prefs: dict) -> List[str]:
        interests = list(EXPERIENCE.get(prefs.get("experience"), []))
        interests.extend(FOLLOW_UP.get(prefs.get("experience"), {}).get(prefs.get("focus"), []))
        style = prefs.get("style")
        if style and style != "Balanced":
            interests.extend(_STYLE_SYNONYMS.get(style.lower(), style).lower().split())
        return list(dict.fromkeys(i.lower() for i in interests if i))

    def _rank_discovery(self, state: dict) -> List[dict]:
        prefs = state["preferences"]
        scope = prefs.get("scope", "both")
        places: Iterable[dict] = self.store.all_places()
        if scope == "hidden":
            places = self.store.hidden_gems()
        elif scope == "popular":
            places = self.store.destinations()
        ranked = recommend_for_preferences(
            places, self._interests(prefs), prefer=scope, limit=max(5, len(list(places)))
        )
        connected = [p for p in ranked if p.get("match_count", 0) > 0]
        if not connected:
            return ranked
        if len(connected) < 3:
            seen = {p.get("slug") for p in connected}
            connected.extend(p for p in ranked if p.get("slug") not in seen)
        return connected

    def _discovery_results(self, state: dict, more: bool = False) -> dict:
        ranked = self._rank_discovery(state)
        offset = int(state.get("shown", 0)) if more else 0
        page = ranked[offset: offset + 5]
        state["shown"] = offset + len(page)
        state["stage"] = "results"
        prefs = state["preferences"]
        if not page:
            return self._response(
                mode="discover", stage="results",
                message="That is everything I found for this combination. Try another mood or focus.",
                actions=[_option("Start Over", "discover", "set_mode")],
            )
        cards = [self._place_card(p, self._why_match(p, prefs)) for p in page]
        actions = []
        if state["shown"] < len(ranked):
            actions.append(_option("Show More", "", "show_more"))
        actions.append(_option("Start Over", "discover", "set_mode"))
        return self._response(
            mode="discover", stage="results",
            message=f"Here are {len(page)} trusted-data matches for {prefs.get('experience', 'your mood').lower()} and {prefs.get('focus', 'your interests').lower()}.",
            cards=cards, actions=actions,
        )

    @staticmethod
    def _why_match(place: dict, prefs: dict) -> str:
        matched = [str(x).title() for x in place.get("matched_interests", [])][:3]
        reason = f"Matches {', '.join(matched)}" if matched else "Strong overall match in our tourism data"
        if place.get("is_hidden_gem"):
            reason += " · curated as a hidden gem"
        elif prefs.get("scope") == "popular":
            reason += " · popular destination"
        return reason

    # ---------------------------------------------------------- hidden gems
    def _hidden(self, state: dict, text: str) -> dict:
        if text:
            location = self._resolve_location(text)
            if location:
                state["destination"] = location
            interests = self._extract_interest_words(text)
            if interests:
                state["preferences"]["interests"] = interests

        location = state.get("destination")
        if not location:
            state["stage"] = "location"
            return self._response(
                mode="hidden", stage="location",
                message="Which destination, city, or state should I explore around? I only label curated records as hidden gems.",
                suggestions=["Near Vadodara", "Meghalaya", "Around Spiti"],
            )

        state["stage"] = "results"
        gems, descriptor = self._hidden_for_location(location, state["preferences"].get("interests", []))
        if not gems:
            return self._response(
                mode="hidden", stage="results",
                message=f"I do not have a verified hidden gem near {location['name']} yet. Try another destination or state.",
                actions=[_option("Choose another place", "hidden", "set_mode")],
            )
        return self._response(
            mode="hidden", stage="results",
            message=f"These are curated hidden gems {descriptor}. Distances shown are straight-line estimates, not live road routes.",
            cards=[self._place_card(g, "Curated hidden gem from Atulya Yatra data") for g in gems[:5]],
            actions=[_option("Choose another place", "hidden", "set_mode"), _option("Start Over", "", "reset")],
        )

    def _hidden_for_location(self, location: dict, interests: Sequence[str]) -> Tuple[List[dict], str]:
        if location["type"] == "state":
            gems = [dict(g) for g in self.store.hidden_gems_in_state(location["slug"])]
            ranked = rank_hidden_gems(gems, radius_km=100, interests=interests)
            return ranked, f"in {location['name']}"
        origin = self.store.get_place(location["slug"])
        if not origin and location.get("type") == "external_location":
            origin = location
        if not origin:
            return [], f"near {location['name']}"
        nearby = self.store.within_radius(origin, 100, only_hidden_gems=True, exclude_slug=origin.get("slug"))
        ranked = rank_hidden_gems(nearby, radius_km=100, interests=interests or origin.get("categories", []))
        return ranked, f"within 100 km of {location['name']}"

    # -------------------------------------------------------------- planner
    def _plan(self, state: dict, text: str) -> dict:
        prefs = state["preferences"]
        if text:
            # Answers to our own chips are preferences, not destinations. Only
            # look for a place when the reply could plausibly name one.
            if text.strip().lower() not in _PLAN_CHIP_LABELS:
                location = self._resolve_location(text)
                if location:
                    state["destination"] = location
            self._parse_plan_preferences(text, prefs, state.get("stage"))
        return self._plan_next(state)

    def _parse_plan_preferences(self, text: str, prefs: dict, stage: Optional[str]) -> None:
        lower = text.lower()
        duration_match = re.search(r"\b(\d{1,2})\s*(?:day|days)\b", lower)
        if duration_match:
            prefs["duration"] = max(1, min(int(duration_match.group(1)), 14))
        elif stage == "duration" and text.strip().isdigit():
            prefs["duration"] = max(1, min(int(text), 14))
        elif _contains(lower, "weekend"):
            # People say "a weekend" far more often than "2 days".
            prefs["duration"] = 2
        elif _contains(lower, "a week") or _contains(lower, "one week"):
            prefs["duration"] = 7
        elif _contains(lower, "fortnight") or _contains(lower, "two weeks"):
            prefs["duration"] = 14

        for word, style in _STYLE_SYNONYMS.items():
            if _contains(lower, word):
                prefs["style"] = style
                break
        for budget in BUDGETS:
            if _contains(lower, budget):
                prefs["budget"] = budget
                break
        if "with my parents" in lower or "parents" in lower:
            prefs["traveller"] = "With parents"
        elif "family" in lower or "kids" in lower or "children" in lower:
            prefs["traveller"] = "Family"
        else:
            for traveller in TRAVELLERS[:4]:
                if _contains(lower, traveller):
                    prefs["traveller"] = traveller
                    break

        if "hidden" in lower:
            prefs["scope"] = "hidden"
        elif "popular" in lower:
            prefs["scope"] = "popular"
        if re.search(r"(?:don'?t|do not|no|avoid|remove)\s+(?:like\s+)?trek", lower):
            self._add_exclusion(prefs, "trekking")

    def _plan_next(self, state: dict) -> dict:
        prefs = state["preferences"]
        location = state.get("destination")
        if not location:
            state["stage"] = "location"
            return self._response(
                mode="plan", stage="location",
                message="Which place in India shall I plan for? Type a city, town or state.",
                suggestions=["Spiti Valley", "Udaipur", "Meghalaya", "Munnar"],
            )
        if not prefs.get("duration"):
            state["stage"] = "duration"
            return self._response(
                mode="plan", stage="duration",
                message=(
                    f"Nice choice — {location['name']}. How many days do you have?\n"
                    "Tap a length below, or just type a number."
                ),
                options=[_option("1 day")] + [_option(f"{n} days") for n in (2, 3, 4, 5, 7)],
            )
        if not prefs.get("style"):
            state["stage"] = "style"
            return self._response(
                mode="plan", stage="style",
                message="What pace or theme should shape the trip?",
                options=[_option(x) for x in PLAN_STYLES],
            )
        if not prefs.get("budget") and not prefs.get("traveller") and not prefs.get("scope"):
            state["stage"] = "details"
            return self._response(
                mode="plan", stage="details",
                message="Optionally add a budget, traveller type, or hidden-gem preference. I will not invent prices.",
                options=[
                    _option("Budget"), _option("Moderate"), _option("Comfort"),
                    _option("Solo"), _option("Family"), _option("With parents"),
                    _option("Hidden Gems"), _option("Skip"),
                ],
            )
        prefs.setdefault("budget", "Skip")
        prefs.setdefault("traveller", "Not specified")
        return self._generate_plan_response(state)

    def _also_check_out(self, state: dict, itinerary: dict) -> List[dict]:
        """Curated places near the trip that the plan itself does not already use.

        Someone who has just been handed an itinerary usually wants to know what
        else is within reach, so the plan ends with a few honest nearby options
        rather than a dead end. Curated data only: no API call, so this adds no
        latency to the turn.
        """
        location = state.get("destination") or {}
        root = self.store.get_place(str(location.get("slug") or ""))
        if not root:
            return []

        planned = {root.get("slug")}
        for day in itinerary.get("days", []) or []:
            for key in ("place", "hidden_gem"):
                entry = day.get(key)
                if isinstance(entry, dict) and entry.get("slug"):
                    planned.add(entry["slug"])

        prefs = state.get("preferences", {})

        def usable(place):
            return (
                place.get("slug") not in planned
                and not self._place_has_excluded_activity(place, prefs)
            )

        nearby = [
            place
            for place in self.store.within_radius(root, 150, exclude_slug=root.get("slug"))
            if usable(place)
        ]

        # A couple of mainstream places plus a couple of quieter ones, so the
        # follow-on options are not all the same kind of trip.
        picks = [p for p in nearby if not p.get("is_hidden_gem")][:2]
        picks += [p for p in nearby if p.get("is_hidden_gem")][:2]

        # A well-packed short trip can legitimately use up everything close by.
        # Rather than showing nothing, widen out to places that share the same
        # character elsewhere in India.
        if len(picks) < 3:
            chosen = {p.get("slug") for p in picks}
            interests = [
                str(value).lower()
                for value in (root.get("categories") or []) + (root.get("tags") or [])
            ]
            similar = recommend_for_preferences(
                [
                    p for p in self.store.all_places()
                    if usable(p) and p.get("slug") not in chosen
                ],
                interests,
                prefer="both",
                limit=6,
            )
            picks += similar[: 4 - len(picks)]

        cards = []
        for place in picks[:4]:
            distance = place.get("distance_km")
            if distance is not None:
                reason = f"About {distance:g} km from {root['name']} — easy to add on"
            elif place.get("state") and place["state"] == root.get("state"):
                reason = f"Elsewhere in {place['state']}"
            else:
                reason = f"Similar in feel to {root['name']}"
            cards.append(self._place_card(place, reason))
        return cards

    def _plan_extras(self, state: dict, itinerary: dict) -> Optional[List[dict]]:
        """The 'you can also check out' section appended to a finished plan."""
        cards = self._also_check_out(state, itinerary)
        if not cards:
            return None
        name = (state.get("destination") or {}).get("name") or "your trip"
        return [
            {
                "key": "also_check_out",
                "title": "You can also check out",
                "subtitle": f"Within reach of {name}, and not already in the plan above.",
                "cards": cards,
            }
        ]

    # Offered after every plan so the next step is always one tap away.
    _PLAN_ACTIONS = (
        ("Add one day", "Give me one extra day", "modify"),
        ("Make it relaxed", "Make the trip more relaxed", "modify"),
        ("Remove trekking", "Remove trekking", "modify"),
        ("Start over", "", "reset"),
    )

    def _existing_plan_response(self, state: dict) -> dict:
        itinerary = state["itinerary"]
        return self._response(
            mode="plan", stage="itinerary",
            message="Welcome back — your itinerary is below, ready to keep editing.",
            itinerary=itinerary,
            sections=self._plan_extras(state, itinerary),
            actions=[_option(*item) for item in self._PLAN_ACTIONS],
            suggestions=["Make Day 2 more relaxed", "Give me one extra day", "Remove trekking"],
        )

    def _generate_plan_response(self, state: dict, *, modified: Optional[str] = None) -> dict:
        state["revision"] = int(state.get("revision", 0)) + 1
        itinerary = self._build_itinerary(state)
        state["itinerary"] = itinerary
        state["stage"] = "itinerary"
        days = itinerary.get("duration_days")
        name = (state.get("destination") or {}).get("name") or "your destination"
        if modified:
            message = f"Updated — {modified}. Here is the revised plan."
        else:
            message = (
                f"Here is your {days}-day plan for {name}. "
                "Every place comes from our own guides, and travel times are clearly "
                "marked as estimates — check live routes before you set off."
            )
        return self._response(
            mode="plan", stage="itinerary",
            message=message,
            itinerary=itinerary,
            sections=self._plan_extras(state, itinerary),
            actions=[_option(*item) for item in self._PLAN_ACTIONS],
            suggestions=["Make Day 2 more relaxed", "Give me one extra day", "Remove trekking"],
        )

    def _build_itinerary(self, state: dict) -> dict:
        prefs = state["preferences"]
        duration = int(prefs["duration"])
        location = state["destination"]
        candidates, root = self._planning_candidates(location, prefs)
        ordered = self._geographic_order(root, candidates, duration)
        if not ordered:
            ordered = [root] if root else []

        days = []
        hidden_used: set = set()
        previous = None
        for index in range(duration):
            place = ordered[index % len(ordered)] if ordered else None
            day = self._build_day(index + 1, place, previous, prefs)
            if place and len(hidden_used) < 2:
                gem = self._nearby_itinerary_gem(place, hidden_used, prefs)
                if gem:
                    hidden_used.add(gem["slug"])
                    day["hidden_gem"] = self._place_card(gem, "Practical curated detour near this day’s route")
            days.append(day)
            previous = place or previous

        return {
            "title": f"{duration}-day {prefs.get('style', 'Balanced').lower()} trip to {location['name']}",
            "destination": location,
            "duration_days": duration,
            "style": prefs.get("style"),
            "budget": None if prefs.get("budget") == "Skip" else prefs.get("budget"),
            "traveller": prefs.get("traveller"),
            "excluded": list(prefs.get("excluded", [])),
            "revision": state["revision"],
            "route_note": "Places are sequenced by nearest-next straight-line distance. Verify live roads, conditions, tickets and timings before travel.",
            "days": days,
        }

    def _planning_candidates(self, location: dict, prefs: dict) -> Tuple[List[dict], Optional[dict]]:
        interests = self._interests(prefs)
        if location["type"] == "state":
            pool = self.store.places_in_state(location["slug"])
            roots = self.store.destinations_in_state(location["slug"]) or pool
            ranked_roots = recommend_for_preferences(roots, interests, prefer=prefs.get("scope", "both"), limit=len(roots) or 1)
            root = ranked_roots[0] if ranked_roots else None
        else:
            root = self.store.get_place(location["slug"])
            if not root and location.get("type") == "external_location":
                root = {
                    **location,
                    "kind": "external_location",
                    "is_hidden_gem": False,
                    "categories": [], "tags": [], "activities": [], "attractions": [],
                    "food": [], "images": {"hero": None, "gallery": []},
                    "short_intro": f"Externally resolved location: {location['name']}.",
                }
            pool = [root] if root else []
            if root:
                nearby = self.store.within_radius(root, 220, exclude_slug=root["slug"])
                pool.extend(
                    p for p in nearby
                    if not root.get("state_slug") or p.get("state_slug") == root.get("state_slug")
                )
        pool = [p for p in pool if p and not self._place_has_excluded_activity(p, prefs)]
        ranked = recommend_for_preferences(pool, interests, prefer=prefs.get("scope", "both"), limit=len(pool) or 1)
        # Keep practical, data-rich candidates; hidden gems are limited separately in day suggestions.
        normal = [p for p in ranked if not p.get("is_hidden_gem")]
        hidden = [p for p in ranked if p.get("is_hidden_gem")][:2]
        merged = list({p["slug"]: p for p in ([root] if root else []) + normal + hidden}.values())
        return merged, root

    @staticmethod
    def _geographic_order(root: Optional[dict], candidates: List[dict], duration: int) -> List[dict]:
        if not candidates:
            return []
        remaining = {p["slug"]: p for p in candidates}
        current = root if root and root.get("slug") in remaining else candidates[0]
        ordered = []
        while remaining and len(ordered) < duration:
            ordered.append(current)
            remaining.pop(current["slug"], None)
            if not remaining:
                break
            def key(place: dict) -> float:
                distance = distance_between(current, place)
                return distance if distance is not None else 1e9
            current = min(remaining.values(), key=key)
        return ordered

    def _build_day(self, number: int, place: Optional[dict], previous: Optional[dict], prefs: dict) -> dict:
        if not place:
            return {
                "day": number, "title": "Flexible day", "place": None,
                "morning": "No verified place detail is available.",
                "afternoon": "Keep this period flexible and verify options locally.",
                "evening": "Rest or make independent plans.",
                "food": None, "travel": "No route estimate available.",
                "stay": "Verify location and access before booking.",
                "safety": "Check current official local advice.", "etiquette": None,
            }
        attractions = [a for a in (place.get("attractions") or []) if not self._text_excluded(a.get("name", ""), prefs)]
        activities = [a for a in (place.get("activities") or []) if not self._text_excluded(a, prefs)]
        morning = self._attraction_text(attractions[0]) if attractions else f"Arrival and orientation in {place['name']}."
        if len(attractions) > 1:
            afternoon = self._attraction_text(attractions[1])
        elif activities:
            afternoon = f"Choose from listed local activities: {', '.join(activities[:2])}."
        else:
            afternoon = "Flexible exploration; no additional verified attraction is stored for this period."
        evening = "Keep the evening flexible for rest and local exploration."
        if prefs.get("style") == "Relaxed" or prefs.get("traveller") == "With parents":
            afternoon = f"Relaxed pace: {afternoon} Allow extra breaks and skip anything strenuous."
        food_items = place.get("food") or []
        food = None
        if food_items:
            first = food_items[0]
            food = {"name": first.get("name"), "type": first.get("type"), "description": first.get("description")}
        safety = place.get("safety") or {}
        tips = safety.get("tips") or []
        etiquette = safety.get("etiquette") or []
        travel_info = place.get("travel_info") or {}
        stay_bits = [f"Use {place['name']} as the day’s base."]
        if travel_info.get("budget"):
            stay_bits.append(str(travel_info["budget"]))
        if travel_info.get("local_transport"):
            stay_bits.append(f"Local transport: {travel_info['local_transport']}")
        return {
            "day": number,
            "title": place["name"],
            "place": self._place_card(place),
            "morning": morning,
            "afternoon": afternoon,
            "evening": evening,
            "food": food,
            "travel": self._travel_estimate(previous, place),
            "stay": " ".join(stay_bits),
            "safety": tips[0] if tips else "No destination-specific safety note is stored; check current official advice.",
            "etiquette": etiquette[0] if etiquette else None,
        }

    @staticmethod
    def _attraction_text(attraction: dict) -> str:
        name = attraction.get("name") or "Listed attraction"
        description = attraction.get("description")
        return f"{name} — {description}" if description else name

    @staticmethod
    def _travel_estimate(previous: Optional[dict], place: dict) -> str:
        if not previous or previous.get("slug") == place.get("slug"):
            return "Local day; use the stored local-transport guidance and verify live routes."
        distance = distance_between(previous, place)
        if distance is None:
            return "Route distance unavailable; verify the live route before departure."
        # Transparent planning range derived from straight-line distance, not a Maps claim.
        low_minutes = max(20, round((distance / 45) * 60 / 10) * 10)
        high_minutes = max(low_minutes + 20, round((distance / 28) * 60 / 10) * 10)
        return (
            f"Approx. {distance:.0f} km straight-line from {previous['name']}; "
            f"allow roughly {ChatAssistant._format_minutes(low_minutes)}–{ChatAssistant._format_minutes(high_minutes)} by road, then verify live Maps."
        )

    @staticmethod
    def _format_minutes(minutes: int) -> str:
        hours, mins = divmod(minutes, 60)
        if not hours:
            return f"{mins} min"
        return f"{hours} hr" + (f" {mins} min" if mins else "")

    def _nearby_itinerary_gem(self, place: dict, used: set, prefs: dict) -> Optional[dict]:
        nearby = self.store.within_radius(place, 100, only_hidden_gems=True, exclude_slug=place.get("slug"))
        nearby = [g for g in nearby if g.get("slug") not in used and not self._place_has_excluded_activity(g, prefs)]
        if not nearby:
            return None
        ranked = rank_hidden_gems(nearby, radius_km=100, interests=self._interests(prefs))
        return ranked[0] if ranked else None

    # --------------------------------------------------------- modifications
    @staticmethod
    def _looks_like_modification(message: str, action: dict) -> bool:
        if action.get("type") == "modify":
            return True
        lower = message.lower()
        return any(word in lower for word in ("make day", "extra day", "add a day", "add one day", "remove trekking", "no trekking", "less day", "remove a day", "more relaxed"))

    def _modify_itinerary(self, state: dict, command: str) -> dict:
        lower = command.lower()
        prefs = state["preferences"]
        description = "the requested change is applied"
        if "extra day" in lower or "add a day" in lower or "add one day" in lower:
            prefs["duration"] = min(14, int(prefs.get("duration", 1)) + 1)
            description = f"the trip now has {prefs['duration']} days"
        elif "less day" in lower or "remove a day" in lower:
            prefs["duration"] = max(1, int(prefs.get("duration", 1)) - 1)
            description = f"the trip now has {prefs['duration']} days"
        if "trek" in lower:
            self._add_exclusion(prefs, "trekking")
            description = "trekking has been removed"
        if "relax" in lower:
            prefs["style"] = "Relaxed"
            description = "the pace is more relaxed"
        response = self._generate_plan_response(state, modified=description)
        day_match = re.search(r"\bday\s*(\d{1,2})\b", lower)
        if day_match and "relax" in lower:
            number = int(day_match.group(1))
            days = state["itinerary"].get("days", [])
            if 1 <= number <= len(days):
                day = days[number - 1]
                day["afternoon"] = f"Relaxed pace: {day['afternoon']} Allow extra breaks and skip anything strenuous."
                day["evening"] = "Unscheduled evening for rest."
                response["itinerary"] = state["itinerary"]
                response["message"] = f"Updated — Day {number} now has a more relaxed pace."
        return response

    @staticmethod
    def _add_exclusion(prefs: dict, value: str) -> None:
        excluded = prefs.setdefault("excluded", [])
        if value not in excluded:
            excluded.append(value)

    @staticmethod
    def _text_excluded(text: object, prefs: dict) -> bool:
        lower = str(text or "").lower()
        return any(ex.lower() in lower for ex in prefs.get("excluded", []))

    def _place_has_excluded_activity(self, place: dict, prefs: dict) -> bool:
        return any(self._text_excluded(a, prefs) for a in (place.get("activities") or []))

    # -------------------------------------------------------------- parsing
    def _resolve_location(self, text: str) -> Optional[dict]:
        lower = text.lower()
        entities = []
        for place in self.store.all_places():
            names = [place.get("name", "")] + list(place.get("aliases") or [])
            for name in names:
                if name and _contains(lower, str(name)):
                    entities.append((len(str(name)), self._location_ref(place)))
        for state in self.store.all_states():
            if state.get("name") and _contains(lower, state["name"]):
                entities.append((len(state["name"]), self._location_ref(state, "state")))
        if entities:
            return max(entities, key=lambda item: item[0])[1]

        location_phrases = re.findall(
            r"\b(?:in|near|around|at|for|to)\s+([a-z][a-z .'-]{1,100})",
            lower,
        )
        candidate_source = location_phrases[-1] if location_phrases else lower
        stripped = re.sub(
            r"\b(plan|trip|itinerary|travel|explore|show|find|suggest|tell|visit|see|places?|attractions?|destinations?|things|anything|what|where|which|how|can|could|would|should|do|about|give|want|need|nearby|food|eat|try|festival|language|culture|tradition|safe|safety|pack|packing|best|time|near|for|please|my|me|i|a|an|the|hidden|gems?|days?|day|with|family|parents|solo|couple|friends|budget|moderate|comfort|balanced|nature|adventure|relaxed|spiritual)\b",
            " ", candidate_source,
        )
        candidate = _normalise(re.sub(r"[^a-z0-9' -]|\d+", " ", stripped))
        # A two-letter fragment prefix-matches a real state, which is how "hi"
        # used to come back as Himachal Pradesh. Anything this short, or that is
        # plainly a greeting, is not a place name.
        if candidate and len(candidate) >= _MIN_LOCATION_QUERY and not _is_small_talk(candidate):
            match = self.search.best_match(candidate)
            threshold = _NORMAL_QUERY_SCORE if len(candidate) >= 5 else _SHORT_QUERY_SCORE
            if match and match.get("score", 0) >= threshold:
                if match["type"] == "state":
                    state = self.store.get_state(match["slug"])
                    return self._location_ref(state, "state") if state else None
                place = self.store.get_place(match["slug"])
                return self._location_ref(place) if place else None
        if candidate and self.place_resolver:
            external = self.place_resolver(candidate[:160])
            if external:
                # State enrichment comes only from curated state records. Google
                # identity and attraction records remain explicitly external.
                address = str(external.get("address") or "").lower()
                for state in self.store.all_states():
                    if state.get("name", "").lower() in address:
                        external["state"] = state["name"]
                        external["state_slug"] = state["slug"]
                        break
                return self._location_ref(external, "external_location")
        return None

    @staticmethod
    def _extract_interest_words(text: str) -> List[str]:
        vocabulary = set(_MOOD_SYNONYMS) | {
            "mountains", "lakes", "waterfalls", "wildlife", "forests", "heritage",
            "history", "architecture", "temples", "monasteries", "trekking", "beaches",
        }
        return [word for word in vocabulary if _contains(text.lower(), word)]

    def _validate_context(self, context: Optional[dict]) -> Optional[dict]:
        if not isinstance(context, dict):
            return None
        kind = str(context.get("type") or "")
        slug = str(context.get("slug") or "")
        if kind in {"destination", "hidden_gem"}:
            place = self.store.get_place(slug)
            return self._location_ref(place) if place else None
        if kind == "state":
            state = self.store.get_state(slug)
            return self._location_ref(state, "state") if state else None
        return None

    @staticmethod
    def _location_ref(record: Optional[dict], forced_type: Optional[str] = None) -> Optional[dict]:
        if not record:
            return None
        kind = forced_type or ("hidden_gem" if record.get("is_hidden_gem") else "destination")
        ref = {"type": kind, "slug": record.get("slug"), "name": record.get("name")}
        if kind == "external_location":
            for field in (
                "lat", "lng", "state", "state_slug", "address", "google_maps_url",
                "google_place_id", "google_photo_names", "photo_attributions", "source",
                "type", "short_intro", "rating", "reviews",
            ):
                if record.get(field) is not None:
                    ref[field] = record.get(field)
        return ref

    @staticmethod
    def _place_card(place: dict, reason: Optional[str] = None) -> dict:
        images = place.get("images") or {}
        return {
            "type": "place",
            "slug": place.get("slug"),
            "name": place.get("name"),
            "state": place.get("state"),
            "state_slug": place.get("state_slug"),
            "is_hidden_gem": bool(place.get("is_hidden_gem")) if place.get("source") != "google_places_api" else False,
            "categories": list(place.get("categories") or []),
            "place_type": place.get("type"),
            "candidate_type": place.get("candidate_type") or ("Curated hidden gem" if place.get("is_hidden_gem") else "Destination"),
            "source": place.get("source") or "atulya_yatra",
            "address": place.get("address") or "",
            "short_intro": place.get("short_intro") or place.get("intro") or "",
            "rating": place.get("rating"),
            "reviews": place.get("reviews"),
            "distance_km": place.get("distance_km"),
            "recommendation_score": place.get("recommendation_score"),
            "image": images.get("hero") or place.get("hero_image"),
            "google_photo_names": list(place.get("google_photo_names") or [])[:1],
            "photo_attributions": list(place.get("photo_attributions") or []),
            "google_maps_url": place.get("google_maps_url") or "",
            "why": reason,
        }

    @staticmethod
    def _response(
        *, mode: Optional[str], stage: str, message: str,
        options: Optional[List[dict]] = None, cards: Optional[List[dict]] = None,
        actions: Optional[List[dict]] = None, suggestions: Optional[List[str]] = None,
        itinerary: Optional[dict] = None, location: Optional[dict] = None,
        sections: Optional[List[dict]] = None, guide: Optional[dict] = None,
        disclaimer: Optional[str] = None,
    ) -> dict:
        response = {
            "mode": mode, "stage": stage, "message": message,
            "options": options or [], "cards": cards or [], "actions": actions or [],
            "suggestions": suggestions or [],
        }
        if itinerary is not None:
            response["itinerary"] = itinerary
        if location is not None:
            response["location"] = location
        if sections is not None:
            response["sections"] = sections
        if guide is not None:
            response["guide"] = guide
        if disclaimer:
            response["disclaimer"] = disclaimer
        return response
