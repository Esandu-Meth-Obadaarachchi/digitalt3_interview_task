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
from typing import Any

from app.extraction.llm.base import LLMProvider, LLMRequest, LLMResponse


def minimal_instance(schema: dict[str, Any], defs: dict[str, Any] | None = None) -> Any:
    """Build the smallest document that satisfies a JSON schema.

    Used as the unscripted default so the whole pipeline can be exercised
    offline with no key and no network: every extraction comes back empty,
    which is a valid answer, and the plumbing either works or it does not.
    """
    defs = defs if defs is not None else schema.get("$defs", {})

    if "$ref" in schema:
        return minimal_instance(defs.get(schema["$ref"].rsplit("/", 1)[-1], {}), defs)
    if "anyOf" in schema:
        return minimal_instance(schema["anyOf"][0], defs)
    if "enum" in schema:
        return schema["enum"][0]

    kind = schema.get("type")
    if kind == "object":
        properties = schema.get("properties", {})
        return {
            name: minimal_instance(sub, defs)
            for name, sub in properties.items()
            if name in schema.get("required", list(properties))
        }
    if kind == "array":
        return []
    if kind == "string":
        return ""
    if kind == "integer":
        return 0
    if kind == "number":
        return 0.0
    if kind == "boolean":
        return False
    return None


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
        """Override the unscripted behaviour with a response derived from the
        request, for tests that need something other than an empty result."""
        self._default = factory
        return self

    def generate(self, request: LLMRequest) -> LLMResponse:
        self.calls.append(request)

        if self._queue:
            text = self._queue.popleft()
        elif self._default is not None:
            text = self._default(request)
        else:
            # Unscripted: answer with the smallest document the schema allows.
            text = json.dumps(minimal_instance(request.json_schema))

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
