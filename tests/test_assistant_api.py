"""Flask API integration checks for assistant session memory and page handoff."""
from __future__ import annotations

import unittest
from urllib.parse import urlparse

from atulya import create_app


class AssistantApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app("development")
        cls.app.config.update(TESTING=True)
        cls.client = cls.app.test_client()

    def test_session_memory_reset_and_destination_context(self):
        first = self.client.post("/api/chat", json={})
        self.assertEqual(first.status_code, 200)
        session_id = first.get_json()["session_id"]
        self.assertTrue(session_id)

        planning = self.client.post(
            "/api/chat",
            json={
                "session_id": session_id,
                "action": {"type": "set_mode", "value": "plan"},
                "context": {"type": "destination", "slug": "vadodara"},
            },
        )
        self.assertEqual(planning.status_code, 200)
        payload = planning.get_json()
        self.assertEqual(payload["mode"], "plan")
        self.assertEqual(payload["stage"], "duration")
        self.assertIn("Vadodara", payload["message"])

        remembered = self.client.post(
            "/api/chat",
            json={
                "session_id": session_id,
                "action": {"type": "answer", "value": "2 days"},
            },
        ).get_json()
        self.assertEqual(remembered["stage"], "style")

        reset = self.client.post("/api/chat/reset", json={"session_id": session_id})
        self.assertEqual(reset.status_code, 200)
        self.assertTrue(reset.get_json()["ok"])

    def test_destination_page_links_out_to_the_assistant(self):
        """The assistant lives on its own page, not as a per-page widget.

        The floating launcher was removed when the detail pages moved to a
        full-bleed hero: the FAB and the hero's "In focus" chip occupied the same
        corner. Detail pages now link to /assistant instead of embedding it.
        """
        response = self.client.get("/destination/vadodara")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Plan a trip here", html)
        self.assertIn('href="/assistant"', html)
        self.assertNotIn("data-chat-root", html)
        self.assertNotIn("js/assistant.js", html)

    def test_geo_api_clamps_invalid_bounds(self):
        nearby = self.client.get("/api/nearby/vadodara?radius=-5&limit=-3")
        self.assertEqual(nearby.status_code, 200)
        self.assertEqual(nearby.get_json()["radius_km"], 1.0)

        gems = self.client.get("/api/hidden-gems/vadodara?radius=999&limit=-2")
        self.assertEqual(gems.status_code, 200)
        self.assertEqual(gems.get_json()["radius_km"], 300.0)
        self.assertLessEqual(len(gems.get_json()["results"]), 1)
    def test_dedicated_assistant_page_hosts_the_only_widget(self):
        page = self.client.get("/assistant")
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        self.assertIn('data-chat-variant="page"', html)
        self.assertIn("Build your itinerary", html)
        self.assertIn("js/assistant.js", html)
        # No floating launcher anywhere: the page variant is the only variant.
        self.assertNotIn("data-chat-fab", html)
        self.assertNotIn('data-chat-variant="floating"', html)

    @staticmethod
    def _decorated_cards(payload):
        """Every place card the API decorates, wherever it sits in the payload.

        A grounded answer attaches its places to `sections`, not to the
        top-level `cards` list, so checking only `cards` silently tests nothing.
        """
        cards = list(payload.get("cards") or [])
        for section in payload.get("sections") or []:
            if isinstance(section, dict):
                cards.extend(section.get("cards") or [])
        if isinstance(payload.get("location"), dict):
            cards.append(payload["location"])
        return cards

    def test_grounded_question_returns_suggestions_with_secure_images(self):
        response = self.client.post(
            "/api/chat", json={"message": "What should I eat in Vadodara?"}
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["mode"], "ask")
        self.assertEqual(payload["stage"], "answer")
        self.assertIn("Sev Usal", payload["message"])

        cards = self._decorated_cards(payload)
        self.assertGreaterEqual(len(cards), 1)

        # The credentials must never reach the browser. A card image is therefore
        # one of:
        #   * a file we host ourselves (/static/images/... — curated photography
        #     downloaded from Wikimedia, the safest case),
        #   * one of our signed internal media routes (how Places photos are
        #     served, so the API key stays server-side), or
        #   * a credential-free URL on a trusted public host.
        # In every case the raw Places photo resource names must be stripped.
        trusted_hosts = ("upload.wikimedia.org", "thumb.wikimedia.org")
        with_images = 0
        for card in cards:
            self.assertNotIn("google_photo_names", card)
            image = card.get("image")
            if not image:
                continue
            with_images += 1
            ours = image.startswith("/media/") or image.startswith("/static/")
            external = image.startswith("https://") and urlparse(image).netloc in trusted_hosts
            self.assertTrue(
                ours or external,
                f"card image is neither self-hosted nor a trusted host: {image!r}",
            )
            self.assertNotIn("key=", image)
        self.assertGreaterEqual(
            with_images, 1, "no card resolved an image, so nothing was verified"
        )

    def test_secure_photo_route_redirects_without_key(self):
        original = self.app.places_resolver

        class FakeResolver:
            enabled = True

            # Mirrors GooglePlacesResolver.photo_uri_for, which the media route
            # calls with an explicit photo index (and a width when one is asked
            # for), so the fake has to accept both.
            @staticmethod
            def photo_uri_for(place, *, index=0, max_width=None):
                return "https://images.example.test/vadodara.jpg"

            @staticmethod
            def _valid_photo_name(value):
                return True

        self.app.places_resolver = FakeResolver()
        try:
            response = self.client.get("/media/place-photo/vadodara")
        finally:
            self.app.places_resolver = original
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "https://images.example.test/vadodara.jpg")
        self.assertNotIn("key=", response.headers["Location"])


if __name__ == "__main__":
    unittest.main()
