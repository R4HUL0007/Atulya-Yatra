"""Focused acceptance scenarios for the Master AI Travel Assistant."""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from atulya.chatbot import ChatAssistant
from atulya.data_store import DataStore
from atulya.search import SearchService

ROOT = Path(__file__).resolve().parents[1]


class AssistantScenarioTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store = DataStore(ROOT / "data")
        cls.store.load()

        def fake_places_resolver(query):
            if "mumbai" not in query.lower():
                return None
            return {
                "slug": "google-test-mumbai",
                "name": "Mumbai",
                "kind": "external_location",
                "is_hidden_gem": False,
                "source": "google_places_api",
                "lat": 19.076,
                "lng": 72.8777,
                "address": "Mumbai, Maharashtra, India",
            }

        cls.assistant = ChatAssistant(
            cls.store, SearchService(cls.store), fake_places_resolver
        )

    def turn(self, state, value, *, action="answer"):
        return self.assistant.respond(
            state, action={"type": action, "value": value}
        )

    def discover(self, experience, focus, scope):
        state = self.assistant.initial_state()
        _, state = self.turn(state, "discover", action="set_mode")
        _, state = self.turn(state, experience)
        _, state = self.turn(state, focus)
        response, state = self.turn(state, scope)
        return response, state

    def plan(self, destination, duration, style, details):
        state = self.assistant.initial_state()
        _, state = self.turn(state, "plan", action="set_mode")
        _, state = self.turn(state, destination)
        _, state = self.turn(state, f"{duration} days")
        _, state = self.turn(state, style)
        response, state = self.turn(state, details)
        self.assertIn("itinerary", response)
        self.assertEqual(response["itinerary"]["duration_days"], duration)
        self.assertEqual(len(response["itinerary"]["days"]), duration)
        return response, state

    def assert_discovery(self, experience, focus, scope):
        response, _ = self.discover(experience, focus, scope)
        self.assertEqual(response["stage"], "results")
        self.assertGreaterEqual(len(response["cards"]), 3)
        self.assertLessEqual(len(response["cards"]), 5)
        self.assertTrue(all(card.get("why") for card in response["cards"]))
        if scope == "Hidden Gems":
            self.assertTrue(all(card["is_hidden_gem"] for card in response["cards"]))
        if scope == "Popular":
            self.assertTrue(all(not card["is_hidden_gem"] for card in response["cards"]))

    def test_nature_mountains_hidden_gems(self):
        self.assert_discovery("Nature", "Mountains", "Hidden Gems")

    def test_adventure_trekking_hidden_gems(self):
        self.assert_discovery("Adventure", "Trekking", "Hidden Gems")

    def test_culture_heritage_popular(self):
        self.assert_discovery("Culture", "Heritage", "Popular")

    def test_peace_lakes_both(self):
        self.assert_discovery("Peace", "Lakes", "Both")

    def test_spiritual_monasteries_hidden_gems(self):
        self.assert_discovery("Spiritual", "Monasteries", "Hidden Gems")

    def test_spiti_five_day_adventure_moderate(self):
        response, _ = self.plan("Spiti", 5, "Adventure", "Moderate")
        self.assertEqual(response["itinerary"]["budget"], "Moderate")

    def test_vadodara_two_day_culture_budget(self):
        response, _ = self.plan("Vadodara", 2, "Culture", "Budget")
        self.assertEqual(response["itinerary"]["budget"], "Budget")

    def test_meghalaya_four_day_nature_hidden_gems(self):
        response, _ = self.plan("Meghalaya", 4, "Nature", "Hidden Gems")
        detours = [day.get("hidden_gem") for day in response["itinerary"]["days"]]
        self.assertLessEqual(len([item for item in detours if item]), 2)
        self.assertTrue(all(not item or item["is_hidden_gem"] for item in detours))

    def test_mumbai_three_day_balanced_family(self):
        response, _ = self.plan("Mumbai", 3, "Balanced", "Family")
        self.assertEqual(response["itinerary"]["traveller"], "Family")

    def test_free_text_memory_and_trip_modifications(self):
        state = self.assistant.initial_state()
        response, state = self.assistant.respond(
            state, message="Plan 3 days in Mumbai, balanced, with my parents"
        )
        # Optional-details prompt may still be needed; Skip completes it.
        if "itinerary" not in response:
            response, state = self.turn(state, "Skip")
        self.assertEqual(response["itinerary"]["traveller"], "With parents")

        response, state = self.assistant.respond(state, message="Make Day 2 more relaxed")
        self.assertIn("Relaxed pace", response["itinerary"]["days"][1]["afternoon"])

        old_duration = response["itinerary"]["duration_days"]
        response, state = self.assistant.respond(state, message="Give me one extra day")
        self.assertEqual(response["itinerary"]["duration_days"], old_duration + 1)

        response, state = self.assistant.respond(state, message="Remove trekking")
        day_text = json.dumps(response["itinerary"]["days"]).lower()
        self.assertNotIn("trekking", day_text)
        self.assertIn("trekking", response["itinerary"]["excluded"])

    def test_hidden_workflow_never_returns_external_candidates(self):
        state = self.assistant.initial_state()
        _, state = self.turn(state, "hidden", action="set_mode")
        response, _ = self.turn(state, "Vadodara")
        self.assertTrue(response["cards"])
        self.assertTrue(all(card["is_hidden_gem"] for card in response["cards"]))
        for card in response["cards"]:
            self.assertNotEqual(self.store.get_place(card["slug"]).get("kind"), "external_attraction")


if __name__ == "__main__":
    unittest.main()
