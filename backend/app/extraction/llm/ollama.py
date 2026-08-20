"""A locally-run model through Ollama.

Present so that provider swappability is demonstrated rather than claimed. The
adapter contract's test is whether a second implementation could be dropped in
by writing one class and changing one line of wiring, and the honest way to
answer that is to have written the second class.

Ollama takes a JSON schema in its `format` field, so structured output is
constrained at the decoder here too, the same as Gemini. Nothing above this
file knows which of the two it is talking to.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from app.errors import LLMError, ProviderUnavailable
from app.extraction.llm.base import LLMProvider, LLMRequest, LLMResponse


class OllamaProvider(LLMProvider):
    name = "ollama"

    def __init__(self, base_url: str, model: str, timeout_seconds: int = 180) -> None:
        self.model = model
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds

    def generate(self, request: LLMRequest) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": self.model,
            "prompt": request.prompt,
            "format": request.json_schema,
            "stream": False,
            "options": {"temperature": request.temperature},
        }
        if request.system:
            payload["system"] = request.system
        if request.max_output_tokens:
            payload["options"]["num_predict"] = request.max_output_tokens

        started = time.perf_counter()
        try:
            response = httpx.post(
                f"{self._base_url}/api/generate", json=payload, timeout=self._timeout
            )
            response.raise_for_status()
        except httpx.ConnectError as exc:
            raise ProviderUnavailable(
                f"cannot reach Ollama at {self._base_url}. Start it with `ollama serve` "
                f"and pull the model with `ollama pull {self.model}`."
            ) from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"ollama call failed: {exc}") from exc

        body = response.json()
        return LLMResponse(
            text=body.get("response", ""),
            provider=self.name,
            model=self.model,
            prompt_tokens=body.get("prompt_eval_count"),
            completion_tokens=body.get("eval_count"),
            latency_ms=int((time.perf_counter() - started) * 1000),
            finish_reason=body.get("done_reason"),
        )

    def available(self) -> tuple[bool, str]:
        try:
            response = httpx.get(f"{self._base_url}/api/tags", timeout=3)
            response.raise_for_status()
        except httpx.HTTPError:
            return False, f"ollama is not reachable at {self._base_url}"

        installed = {m.get("name", "") for m in response.json().get("models", [])}
        if self.model not in installed and f"{self.model}:latest" not in installed:
            return False, f"ollama is running but {self.model} is not pulled"
        return True, f"ollama {self.model}"
