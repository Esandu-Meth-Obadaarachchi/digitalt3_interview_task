"""Per-attempt accounting for model calls.

One row per attempt, retries included, so three things are measured rather
than asserted: how often the model needs repairing, how often the cache
answers, and what a source costs in tokens and wall-clock time.

The rubric's robustness criterion asks that malformed output be caught and
retried against a schema. `outcome` records which failure mode occurred, so
"the retry loop works" becomes a number in the eval output.
"""

from __future__ import annotations

from enum import StrEnum

from app.models.common import StrictModel


class CallOutcome(StrEnum):
    OK = "ok"
    PARSE_ERROR = "parse_error"              # response was not JSON
    VALIDATION_ERROR = "validation_error"    # JSON, but not the schema
    QUOTE_UNVERIFIED = "quote_unverified"    # schema ok, quote not in the source
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"
    ERROR = "error"


class LLMCall(StrictModel):
    """One attempt against a provider."""

    id: str          #: this attempt
    call_id: str     #: the logical request this attempt belongs to
    capability: str
    provider: str
    model: str
    attempt: int = 1
    outcome: CallOutcome
    source_id: str | None = None
    prompt_version: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    latency_ms: int | None = None
    cache_hit: bool = False
    error: str | None = None
    created_at: str


class UsageSummary(StrictModel):
    """Aggregated view over llm_calls, for the eval harness and the UI."""

    calls: int = 0
    attempts: int = 0
    cache_hits: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_latency_ms: int = 0
    outcomes: dict[str, int] = {}

    @property
    def cache_hit_rate(self) -> float:
        return self.cache_hits / self.attempts if self.attempts else 0.0

    @property
    def retry_rate(self) -> float:
        """Share of attempts that were repairs of a previous failure."""
        return (self.attempts - self.calls) / self.attempts if self.attempts else 0.0
