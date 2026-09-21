"""Optional language-model layer for conversational phrasing.

What this does and, more importantly, what it does not
-----------------------------------------------------
The assistant's *facts* always come from the curated data store and the Places
API. This module only changes **how those facts are worded**, plus small talk
that involves no facts at all. It is never asked to name a place, invent a price,
a timing, a distance or a festival date: every itinerary, card and guide on the
page is still assembled deterministically by :mod:`atulya.chatbot`.

That boundary is deliberate. A travel site that invents opening hours or
distances is worse than one that phrases things plainly, so the model is given a
FACTS block and told to answer only from it. If it returns nothing, errors, or
times out, the caller falls back to the original deterministic sentence and the
conversation continues unaffected.

Configuration
-------------
Set one of these in ``.env``; the provider is detected automatically:

    GEMINI_API_KEY=...        # or GOOGLE_API_KEY
    OPENAI_API_KEY=...

Optional overrides::

    ATULYA_LLM_PROVIDER=gemini|openai|none
    ATULYA_LLM_MODEL=<model name>
    ATULYA_LLM_TIMEOUT=20

Privacy note: when a key is configured, the visitor's message and the curated
facts relevant to it are sent to that provider. With no key, nothing leaves the
server and the assistant behaves exactly as before.
"""
from __future__ import annotations

from typing import Optional

import requests

GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
OPENAI_ENDPOINT = "https://api.openai.com/v1/chat/completions"

# Floating aliases rather than pinned versions. Pinned Gemini models retire and
# then answer 404 -- "gemini-2.0-flash" did exactly that -- which would quietly
# switch this layer off. Phrasing is not sensitive to the exact model, so
# tracking the current release matters more here than pinning a version.
#
# The *lite* alias is chosen deliberately: rewording facts needs no reasoning,
# and measured against this API it answers in 1-2s where the full flash models
# spend 7-12s thinking first. Set ATULYA_LLM_MODEL to pin something else.
DEFAULT_MODELS = {
    "gemini": "gemini-flash-lite-latest",
    "openai": "gpt-4o-mini",
}

# Used to recover when a pinned model has been retired.
GEMINI_FALLBACK_MODEL = "gemini-flash-lite-latest"

# Generous enough that a "thinking" model pinned via ATULYA_LLM_MODEL can spend
# tokens reasoning and still have room to answer. Thinking tokens are drawn from
# this same budget, so a tight limit truncates the visible reply mid-sentence.
MAX_OUTPUT_TOKENS = 1024
MAX_REPLY_CHARS = 1200


class LLMClient:
    """Small, provider-agnostic text completion client that fails soft."""

    def __init__(
        self,
        provider: str = "",
        api_key: str = "",
        model: str = "",
        *,
        timeout: float = 20.0,
        session: Optional[requests.Session] = None,
    ):
        self.provider = (provider or "").strip().lower()
        self.api_key = (api_key or "").strip()
        self.model = (model or "").strip() or DEFAULT_MODELS.get(self.provider, "")
        self.timeout = max(2.0, float(timeout or 20.0))
        self.session = session or requests.Session()
        # Set when a call fails, so callers/tests can see why without the key
        # ever appearing in a log line.
        self.last_error: Optional[str] = None

    @classmethod
    def from_config(cls, config, *, session: Optional[requests.Session] = None) -> "LLMClient":
        """Build a client from app config, auto-detecting the provider."""
        provider = str(config.get("LLM_PROVIDER") or "").strip().lower()
        gemini_key = str(config.get("GEMINI_API_KEY") or "").strip()
        openai_key = str(config.get("OPENAI_API_KEY") or "").strip()

        if provider == "none":
            return cls(session=session)
        if not provider:
            provider = "gemini" if gemini_key else ("openai" if openai_key else "")

        key = gemini_key if provider == "gemini" else openai_key if provider == "openai" else ""
        return cls(
            provider=provider,
            api_key=key,
            model=str(config.get("LLM_MODEL") or ""),
            timeout=float(config.get("LLM_TIMEOUT_SECONDS") or 12.0),
            session=session,
        )

    @property
    def enabled(self) -> bool:
        return bool(self.provider in DEFAULT_MODELS and self.api_key and self.model)

    def complete(
        self,
        *,
        system: str,
        user: str,
        temperature: float = 0.4,
        max_output_tokens: int = MAX_OUTPUT_TOKENS,
    ) -> Optional[str]:
        """Return model text, or ``None`` so the caller uses its own wording."""
        if not self.enabled or not user.strip():
            return None
        self.last_error = None
        try:
            if self.provider == "gemini":
                text = self._gemini(system, user, temperature, max_output_tokens)
            else:
                text = self._openai(system, user, temperature, max_output_tokens)
        except requests.RequestException as exc:
            self.last_error = f"request failed: {type(exc).__name__}"
            return None
        except (ValueError, KeyError, TypeError) as exc:
            self.last_error = f"unexpected response: {type(exc).__name__}"
            return None

        cleaned = " ".join(str(text or "").split())
        if not cleaned:
            # _gemini may already have recorded a more specific reason.
            self.last_error = self.last_error or "empty reply"
            return None
        return cleaned[:MAX_REPLY_CHARS]

    # ------------------------------------------------------------- providers
    def _gemini(self, system: str, user: str, temperature: float, max_tokens: int) -> str:
        response = self._gemini_post(self.model, system, user, temperature, max_tokens)

        # A retired or misspelled model name answers 404. Recover once onto the
        # current flash alias so the feature degrades to "slightly different
        # wording" instead of switching itself off.
        if response.status_code == 404 and self.model != GEMINI_FALLBACK_MODEL:
            self.model = GEMINI_FALLBACK_MODEL
            response = self._gemini_post(self.model, system, user, temperature, max_tokens)

        response.raise_for_status()
        candidates = response.json().get("candidates") or []
        if not candidates:
            return ""

        # A reply cut off at the token ceiling ends mid-sentence. Showing a
        # visitor half a sentence is worse than showing our own complete one, so
        # treat truncation as a failure and let the caller fall back.
        if candidates[0].get("finishReason") == "MAX_TOKENS":
            self.last_error = "reply truncated at the token limit"
            return ""

        parts = ((candidates[0].get("content") or {}).get("parts")) or []
        return " ".join(str(part.get("text") or "") for part in parts)

    def _gemini_post(
        self, model: str, system: str, user: str, temperature: float, max_tokens: int
    ) -> requests.Response:
        return self.session.post(
            GEMINI_ENDPOINT.format(model=model),
            # The key travels in a header, never in the URL or query string, so
            # it cannot end up in access logs or referrers.
            headers={"x-goog-api-key": self.api_key, "Content-Type": "application/json"},
            json={
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": user}]}],
                "generationConfig": {
                    "temperature": temperature,
                    "maxOutputTokens": max_tokens,
                },
            },
            timeout=self.timeout,
        )

    def _openai(self, system: str, user: str, temperature: float, max_tokens: int) -> str:
        response = self.session.post(
            OPENAI_ENDPOINT,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": temperature,
                "max_tokens": max_tokens,
            },
            timeout=self.timeout,
        )
        response.raise_for_status()
        choices = response.json().get("choices") or []
        if not choices:
            return ""
        return str((choices[0].get("message") or {}).get("content") or "")
