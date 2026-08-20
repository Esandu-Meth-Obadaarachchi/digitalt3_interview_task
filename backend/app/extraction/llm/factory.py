"""One function that turns configuration into a provider.

Nothing else in the application constructs a provider. Adding a third model
means writing one class and adding one branch here, which is exactly the test
the adapter contract applies.
"""

from __future__ import annotations

from app.config import Settings, get_settings
from app.extraction.llm.base import LLMProvider
from app.extraction.llm.fake import FakeProvider
from app.extraction.llm.gemini import GeminiProvider
from app.extraction.llm.ollama import OllamaProvider

#: Set by tests to pin a scripted provider without touching configuration.
_override: LLMProvider | None = None


def set_provider_override(provider: LLMProvider | None) -> None:
    global _override
    _override = provider


def get_llm_provider(settings: Settings | None = None) -> LLMProvider:
    if _override is not None:
        return _override

    cfg = settings or get_settings()

    if cfg.llm_provider == "gemini":
        return GeminiProvider(cfg.gemini_api_key, cfg.gemini_model, cfg.llm_timeout_seconds)
    if cfg.llm_provider == "ollama":
        return OllamaProvider(cfg.ollama_base_url, cfg.ollama_model, cfg.llm_timeout_seconds)
    if cfg.llm_provider == "fake":
        return FakeProvider()

    raise ValueError(f"unknown LLM_PROVIDER: {cfg.llm_provider}")
