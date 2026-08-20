"""A deterministic provider for the test suite.

Not a mock in the unittest sense: it is a real implementation of the interface
that answers from a scripted table, so tests exercise the whole wrapper,
including the retry and repair loop, without a network call or an API key.

Scripting a malformed response is how the retry loop is tested at all. There is
no other way to make a live model reliably return broken JSON on demand.
"""

from __future__ import annotations

import json
from collections import deque
from collections.abc import Callable

from app.errors import ProviderUnavailable
from app.extraction.llm.base import LLMProvider, LLMRequest, LLMResponse


class FakeProvider(LLMProvider):
    name = "fake"

    def __init__(self, model: str = "fake-1", responses: list[str] | None = None) -> None:
        self.model = model
        self._queue: deque[str] = deque(responses or [])
        self._default: Callable[[LLMRequest], str] | None = None
        self.calls: list[LLMRequest] = []

    def queue(self, *responses: str) -> FakeProvider:
        """Script the next N responses, in order."""
        self._queue.extend(responses)
        return self

    def queue_json(self, *payloads: object) -> FakeProvider:
        self._queue.extend(json.dumps(p) for p in payloads)
        return self

    def default(self, factory: Callable[[LLMRequest], str]) -> FakeProvider:
        """Answer any unscripted call by deriving a response from the request."""
        self._default = factory
        return self

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.calls.append(request)

        if self._queue:
            text = self._queue.popleft()
        elif self._default is not None:
            text = self._default(request)
        else:
            raise ProviderUnavailable("FakeProvider has no scripted response left")

        return LLMResponse(
            text=text,
            provider=self.name,
            model=self.model,
            prompt_tokens=len(request.prompt) // 4,
            completion_tokens=len(text) // 4,
            latency_ms=0,
        )

    def available(self) -> tuple[bool, str]:
        return True, "deterministic stub, no network"
