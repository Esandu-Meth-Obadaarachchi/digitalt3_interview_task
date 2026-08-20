"""Provider selection and availability.

The adapter contract asks: could a real integration be dropped in by writing
one new class and changing one line of wiring, with zero changes to agent
logic? Three providers implement `LLMProvider` and the factory is the only
place that knows which is which, so the answer is demonstrable rather than
claimed.
"""

from __future__ import annotations

import pytest

from app.config import Settings, get_settings
from app.extraction.llm.base import LLMProvider
from app.extraction.llm.factory import get_llm_provider, set_provider_override
from app.extraction.llm.fake import FakeProvider
from app.extraction.llm.gemini import GeminiProvider
from app.extraction.llm.ollama import OllamaProvider
from app.extraction.llm.rate_limit import RateLimiter, backoff_seconds


def _settings(**overrides) -> Settings:
    get_settings.cache_clear()
    return Settings(**overrides)


@pytest.mark.parametrize(
    ("provider_name", "expected"),
    [("gemini", GeminiProvider), ("ollama", OllamaProvider), ("fake", FakeProvider)],
)
def test_the_factory_returns_the_configured_provider(provider_name, expected):
    """Swapping the model is one value in .env, not a code change."""
    set_provider_override(None)
    provider = get_llm_provider(_settings(llm_provider=provider_name, gemini_api_key="x"))
    assert isinstance(provider, expected)
    assert isinstance(provider, LLMProvider)


def test_an_unknown_provider_fails_loudly():
    with pytest.raises(Exception):
        get_llm_provider(_settings(llm_provider="not-a-provider"))


def test_gemini_reports_a_missing_key_rather_than_failing_mid_run():
    usable, reason = GeminiProvider("", "gemini-2.0-flash").available()
    assert usable is False
    assert "GEMINI_API_KEY" in reason


def test_gemini_with_a_key_reports_itself_usable():
    usable, reason = GeminiProvider("a-key", "gemini-2.0-flash").available()
    assert usable is True
    assert "gemini-2.0-flash" in reason


def test_ollama_reports_an_unreachable_daemon_with_the_fix():
    usable, reason = OllamaProvider("http://127.0.0.1:59999", "llama3.1:8b").available()
    assert usable is False
    assert "not reachable" in reason


def test_calling_an_unreachable_ollama_explains_how_to_start_it():
    from app.errors import ProviderUnavailable
    from app.extraction.llm.base import LLMRequest

    provider = OllamaProvider("http://127.0.0.1:59999", "llama3.1:8b")
    with pytest.raises(ProviderUnavailable) as excinfo:
        provider.generate(LLMRequest(prompt="hello", json_schema={"type": "object"}))

    assert "ollama serve" in str(excinfo.value)
    assert "ollama pull llama3.1:8b" in str(excinfo.value)


def test_every_provider_satisfies_the_same_narrow_interface():
    for provider in (GeminiProvider("k", "m"), OllamaProvider("http://x", "m"), FakeProvider()):
        assert callable(provider.generate)
        assert callable(provider.available)
        assert provider.describe() == f"{provider.name}:{provider.model}"


def test_the_rate_limiter_spaces_calls_and_reports_what_it_waited():
    limiter = RateLimiter(requests_per_minute=6000)  # 10ms apart
    for _ in range(3):
        limiter.acquire()
    assert limiter.wait_count >= 1
    assert limiter.waited_seconds > 0


def test_a_zero_rate_limit_never_blocks():
    limiter = RateLimiter(requests_per_minute=0)
    assert limiter.acquire() == 0.0
    assert limiter.wait_count == 0


def test_backoff_grows_and_is_capped():
    delays = [backoff_seconds(n) for n in range(1, 8)]
    assert delays == sorted(delays)
    assert max(delays) <= 30.0
