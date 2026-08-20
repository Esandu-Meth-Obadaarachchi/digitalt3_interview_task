"""Google Gemini via the google-genai SDK.

Structured output is enforced at the decoder, not asked for in the prompt:
`response_mime_type="application/json"` plus `response_schema` means the model
is constrained to emit JSON matching the schema. The wrapper still validates
every response against the Pydantic model, because a constrained decoder makes
malformed output unlikely rather than impossible, and because the same wrapper
has to behave identically on a provider without that feature.

Free tier limits are part of the exercise. A 429 is raised as RateLimitedError
so the wrapper can back off, rather than being retried blindly.
"""

from __future__ import annotations

import time
from typing import Any

from app.errors import LLMError, ProviderUnavailable, RateLimitedError
from app.extraction.llm.base import LLMProvider, LLMRequest, LLMResponse

# Substrings that identify a rate-limit or quota refusal across SDK versions.
_RATE_LIMIT_MARKERS = ("429", "resource_exhausted", "rate limit", "quota")


class GeminiProvider(LLMProvider):
    name = "gemini"

    def __init__(self, api_key: str, model: str, timeout_seconds: int = 90) -> None:
        self.model = model
        self._api_key = api_key
        self._timeout_ms = timeout_seconds * 1000
        self._client: Any | None = None

    def _get_client(self) -> Any:
        if self._client is None:
            if not self._api_key:
                raise ProviderUnavailable(
                    "GEMINI_API_KEY is not set. Add it to .env, or set LLM_PROVIDER=ollama "
                    "to run against a local model instead."
                )
            from google import genai
            from google.genai import types

            self._client = genai.Client(
                api_key=self._api_key,
                http_options=types.HttpOptions(timeout=self._timeout_ms),
            )
        return self._client

    def generate(self, request: LLMRequest) -> LLMResponse:
        from google.genai import types

        client = self._get_client()
        config = types.GenerateContentConfig(
            temperature=request.temperature,
            response_mime_type="application/json",
            response_json_schema=request.json_schema,
            system_instruction=request.system,
            max_output_tokens=request.max_output_tokens,
        )

        started = time.perf_counter()
        try:
            response = client.models.generate_content(
                model=self.model, contents=request.prompt, config=config
            )
        except Exception as exc:  # SDK exception types vary between versions
            message = str(exc).lower()
            if any(marker in message for marker in _RATE_LIMIT_MARKERS):
                raise RateLimitedError(f"gemini rate limit or quota reached: {exc}") from exc
            raise LLMError(f"gemini call failed: {exc}") from exc

        latency_ms = int((time.perf_counter() - started) * 1000)
        usage = getattr(response, "usage_metadata", None)
        candidate = (getattr(response, "candidates", None) or [None])[0]

        return LLMResponse(
            text=response.text or "",
            provider=self.name,
            model=self.model,
            prompt_tokens=getattr(usage, "prompt_token_count", None),
            completion_tokens=getattr(usage, "candidates_token_count", None),
            latency_ms=latency_ms,
            finish_reason=str(getattr(candidate, "finish_reason", "") or "") or None,
        )

    def available(self) -> tuple[bool, str]:
        if not self._api_key:
            return False, "GEMINI_API_KEY is not set"
        try:
            import google.genai  # noqa: F401
        except ImportError:
            return False, "google-genai is not installed"
        return True, f"gemini {self.model}"
