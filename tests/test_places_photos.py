"""Credential-safety and caching tests for Google Places photo delivery."""
from __future__ import annotations

import unittest

from atulya.places import GooglePlacesResolver


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self):
        self.posts = []
        self.gets = []

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return FakeResponse({
            "places": [{
                "id": "place-1",
                "displayName": {"text": "Vadodara"},
                "formattedAddress": "Vadodara, Gujarat, India",
                "location": {"latitude": 22.3, "longitude": 73.18},
                "primaryType": "locality",
                "photos": [{
                    "name": "places/place-1/photos/photo-1",
                    "authorAttributions": [{"displayName": "Example photographer"}],
                }],
            }]
        })

    def get(self, url, **kwargs):
        self.gets.append((url, kwargs))
        return FakeResponse({"photoUri": "https://images.example.test/photo.jpg"})


class PlacesPhotoTests(unittest.TestCase):
    def test_photo_lookup_uses_header_only_and_caches(self):
        session = FakeSession()
        resolver = GooglePlacesResolver("test-secret-key", session=session)
        place = {"name": "Vadodara", "state": "Gujarat"}

        first = resolver.photo_uri_for(place)
        second = resolver.photo_uri_for(place)

        self.assertEqual(first, "https://images.example.test/photo.jpg")
        self.assertEqual(second, first)
        self.assertEqual(len(session.posts), 1)
        self.assertEqual(len(session.gets), 1)
        search_url, search_kwargs = session.posts[0]
        media_url, media_kwargs = session.gets[0]
        self.assertNotIn("test-secret-key", search_url)
        self.assertNotIn("test-secret-key", media_url)
        self.assertEqual(search_kwargs["headers"]["X-Goog-Api-Key"], "test-secret-key")
        self.assertEqual(media_kwargs["headers"]["X-Goog-Api-Key"], "test-secret-key")

    def test_persisted_resource_avoids_text_search(self):
        session = FakeSession()
        resolver = GooglePlacesResolver("test-secret-key", session=session)
        uri = resolver.photo_uri_for({
            "name": "Stored place",
            "google_photo_names": ["places/place-2/photos/photo-2"],
        })
        self.assertEqual(uri, "https://images.example.test/photo.jpg")
        self.assertEqual(session.posts, [])
        self.assertEqual(len(session.gets), 1)

    def test_invalid_resource_is_rejected(self):
        resolver = GooglePlacesResolver("test-secret-key", session=FakeSession())
        self.assertFalse(resolver._valid_photo_name("https://evil.example/photo"))
        self.assertFalse(resolver._valid_photo_name("places/x/not-photos/y"))


if __name__ == "__main__":
    unittest.main()
