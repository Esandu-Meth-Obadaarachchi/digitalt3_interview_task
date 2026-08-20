"""The wrapper every model call goes through.

The retry-and-repair loop is the part the brief calls mandatory, so it is
tested against scripted responses. A live model cannot be made to return
malformed JSON on demand, which is why FakeProvider exists as a real
implementation of the interface rather than as a mock.
"""

from __future__ import annotations

import json

import pytest
from pydantic import Field

from app.db import database
from app.db.repositories import llm_calls as llm_call_repo
from app.errors import ProviderUnavailable, RateLimitedError, SchemaValidationError
from app.extraction.llm.cache import ResponseCache, cache_key
from app.extraction.llm.base import LLMRequest
from app.extraction.llm.client import call_structured
from app.extraction.llm.fake import FakeProvider
from app.models.common import StrictModel
from app.models.telemetry import CallOutcome


class Item(StrictModel):
    what: str
    owner: str
    quote: str
    confidence: float = Field(ge=0.0, le=1.0)


class ItemList(StrictModel):
    items: list[Item]


VALID = {
    "items": [
        {"what": "Finish the auth refactor", "owner": "Priya Sharma",
         "quote": "I can have the refactor done with tests by Friday", "confidence": 0.9}
    ]
}


def _call(provider, settings, **kwargs):
    return call_structured("extract_actions", "extract from this", ItemList,
                           provider=provider, settings=settings, **kwargs)


# --- the happy path ----------------------------------------------------------


def test_a_valid_response_is_returned_as_a_validated_model(settings):
    provider = FakeProvider().queue_json(VALID)
    result = _call(provider, settings)

    assert isinstance(result, ItemList)
    assert result.items[0].owner == "Priya Sharma"
    assert len(provider.calls) == 1


def test_the_json_schema_is_sent_to_the_provider(settings):
    """Structured output is constrained at the decoder, not requested in prose."""
    provider = FakeProvider().queue_json(VALID)
    _call(provider, settings)

    schema = provider.calls[0].json_schema
    assert schema["type"] == "object"
    assert "items" in schema["properties"]


# --- the repair loop ---------------------------------------------------------


def test_malformed_json_is_retried_with_the_parse_error_fed_back(settings):
    provider = FakeProvider().queue("this is not json at all", json.dumps(VALID))
    result = _call(provider, settings)

    assert result.items[0].owner == "Priya Sharma"
    assert len(provider.calls) == 2

    repair = provider.calls[1].prompt
    assert "YOUR PREVIOUS RESPONSE WAS REJECTED" in repair
    assert "not valid JSON" in repair


def test_a_schema_violation_is_retried_with_the_field_error_fed_back(settings):
    """The model is told which field failed and why, not merely 'try again'."""
    provider = FakeProvider().queue(
        json.dumps({"items": [{"what": "x", "quote": "q", "confidence": 0.5}]}),  # owner missing
        json.dumps(VALID),
    )
    result = _call(provider, settings)

    assert len(provider.calls) == 2
    assert "owner" in provider.calls[1].prompt
    assert result.items[0].owner == "Priya Sharma"


def test_an_out_of_range_value_is_caught_by_the_contract(settings):
    provider = FakeProvider().queue(
        json.dumps({"items": [{"what": "x", "owner": "y", "quote": "q", "confidence": 4.2}]}),
        json.dumps(VALID),
    )
    _call(provider, settings)
    assert "confidence" in provider.calls[1].prompt


def test_an_invented_field_is_a_validation_failure_not_a_silent_drop(settings):
    """extra='forbid' on the contracts means a model drifting from the schema
    triggers a repair rather than having its extra output quietly discarded."""
    provider = FakeProvider().queue(
        json.dumps({"items": [{"what": "x", "owner": "y", "quote": "q",
                               "confidence": 0.5, "ticket_id": "MOCK-1"}]}),
        json.dumps(VALID),
    )
    _call(provider, settings)
    assert "ticket_id" in provider.calls[1].prompt


def test_a_custom_validator_joins_the_same_retry_loop(settings):
    """This is how quote verification is wired in Phase 3: a quote that is not
    a literal substring of the transcript is a validation failure like any
    other, and the model is told which quote failed."""
    transcript = "I can have the refactor done with tests by Friday"

    def quotes_must_be_verbatim(value: ItemList) -> str | None:
        for item in value.items:
            if item.quote not in transcript:
                return f"the quote {item.quote!r} is not a literal substring of the transcript"
        return None

    fabricated = {"items": [{"what": "x", "owner": "y",
                             "quote": "I will definitely finish by Friday", "confidence": 0.9}]}
    provider = FakeProvider().queue(json.dumps(fabricated), json.dumps(VALID))

    result = _call(provider, settings, validators=[quotes_must_be_verbatim])

    assert len(provider.calls) == 2
    assert "not a literal substring" in provider.calls[1].prompt
    assert result.items[0].quote in transcript


def test_a_markdown_fence_around_the_json_is_tolerated(settings):
    provider = FakeProvider().queue(f"```json\n{json.dumps(VALID)}\n```")
    assert _call(provider, settings).items[0].owner == "Priya Sharma"


def test_exhausting_every_attempt_raises_with_each_failure_named(settings):
    provider = FakeProvider().queue("nope", "still nope", "nope again")

    with pytest.raises(SchemaValidationError) as excinfo:
        _call(provider, settings, max_retries=3)

    message = str(excinfo.value)
    assert "3 attempts" in message
    assert message.count("attempt") >= 3
    assert len(provider.calls) == 3


# --- accounting --------------------------------------------------------------


def test_every_attempt_is_recorded_so_the_retry_rate_is_measurable(settings):
    provider = FakeProvider().queue("not json", json.dumps(VALID))
    _call(provider, settings, source_id="src-1", prompt_version="1+abc123")

    with database.connect(settings) as conn:
        calls = llm_call_repo.recent(conn)
        summary = llm_call_repo.summarise(conn)

    assert len(calls) == 2
    assert {c.outcome for c in calls} == {CallOutcome.PARSE_ERROR, CallOutcome.OK}
    assert [c.attempt for c in sorted(calls, key=lambda c: c.attempt)] == [1, 2]
    assert all(c.prompt_version == "1+abc123" for c in calls)
    assert all(c.source_id == "src-1" for c in calls)

    assert summary.attempts == 2
    assert summary.calls == 1
    assert summary.retry_rate == 0.5


def test_accounting_failure_never_breaks_an_extraction(settings, monkeypatch):
    """Telemetry is not load-bearing."""
    def explode(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(llm_call_repo, "record_call", explode)
    provider = FakeProvider().queue_json(VALID)
    assert _call(provider, settings).items[0].owner == "Priya Sharma"


# --- provider failures -------------------------------------------------------


def test_an_unavailable_provider_fails_before_any_call(settings):
    class Unavailable(FakeProvider):
        def available(self):
            return False, "GEMINI_API_KEY is not set"

    with pytest.raises(ProviderUnavailable, match="GEMINI_API_KEY"):
        _call(Unavailable(), settings)


def test_a_rate_limit_on_the_final_attempt_is_raised_not_swallowed(settings):
    class Limited(FakeProvider):
        def generate(self, request):
            raise RateLimitedError("429 resource exhausted")

    with pytest.raises(RateLimitedError):
        _call(Limited(), settings, max_retries=1)

    with database.connect(settings) as conn:
        assert llm_call_repo.recent(conn)[0].outcome is CallOutcome.RATE_LIMITED


# --- caching -----------------------------------------------------------------


def test_the_cache_key_changes_when_the_prompt_version_changes(settings):
    """A cached result can never be attributed to a prompt that did not
    produce it."""
    request = LLMRequest(prompt="p", json_schema={"type": "object"})
    assert cache_key("gemini", "m", request, "1+aaa") != cache_key("gemini", "m", request, "2+bbb")


def test_the_cache_key_changes_when_the_schema_changes(settings):
    a = LLMRequest(prompt="p", json_schema={"type": "object", "properties": {"a": {}}})
    b = LLMRequest(prompt="p", json_schema={"type": "object", "properties": {"b": {}}})
    assert cache_key("gemini", "m", a, "1") != cache_key("gemini", "m", b, "1")


def test_a_cached_response_is_reused_and_flagged_as_a_hit(settings, monkeypatch):
    monkeypatch.setenv("LLM_CACHE_ENABLED", "true")
    from app.config import get_settings

    get_settings.cache_clear()
    cfg = get_settings()

    provider = FakeProvider().queue_json(VALID)
    call_structured("extract_actions", "same prompt", ItemList, provider=provider, settings=cfg)
    assert len(provider.calls) == 1

    # Nothing left in the queue: a second live call would raise.
    result = call_structured("extract_actions", "same prompt", ItemList, provider=provider, settings=cfg)
    assert result.items[0].owner == "Priya Sharma"
    assert len(provider.calls) == 1

    with database.connect(cfg) as conn:
        assert llm_call_repo.summarise(conn).cache_hits == 1

    assert ResponseCache(cfg.llm_cache_dir).size() == 1
