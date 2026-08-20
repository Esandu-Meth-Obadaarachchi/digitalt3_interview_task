"""The provider interface.

Everything above this line talks to `LLMProvider`. Nothing above it imports a
concrete provider, so swapping Gemini for a local model, or for the
deterministic stub the test suite uses, is a change to one configuration value.

The interface is deliberately narrow: one method that takes a prompt and a JSON
schema and returns text plus accounting. Streaming, chat history, function
calling and embeddings are all absent because no capability in this build needs
them, and an interface that promises more than it is asked for is a claim that
will not survive review.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from app.models.common import StrictModel


class LLMRequest(StrictModel):
    """One call to a model."""

    prompt: str
    json_schema: dict[str, Any]
    system: str | None = None
    temperature: float = 0.0
    max_output_tokens: int | None = None


class LLMResponse(StrictModel):
    """What came back, plus what it cost.

    Token counts and latency are recorded on every attempt so the retry rate,
    the cache hit rate and the per-source cost are measured rather than
    estimated.
    """

    text: str
    provider: str
    model: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    latency_ms: int = 0
    cache_hit: bool = False
    finish_reason: str | None = None


class LLMProvider(ABC):
    """A model that returns JSON conforming to a supplied schema."""

    name: str
    model: str

    @abstractmethod
    def generate(self, request: LLMRequest) -> LLMResponse:
        """Call the model once. Raises RateLimitedError or ProviderUnavailable.

        Implementations do not retry. Retrying, repairing and validating are
        the wrapper's job, so that behaviour is identical across providers.
        """

    @abstractmethod
    def available(self) -> tuple[bool, str]:
        """(usable, reason). Checked at startup and by `make check-env` so a
        missing key is reported before an extraction run, not during it."""

    def describe(self) -> str:
        return f"{self.name}:{self.model}"
