"""The single entry point for every model call in the system.

Nothing else calls a provider. Everything goes through `call_structured`, so
retrying, repairing, validating, rate limiting, caching and accounting behave
identically whichever model is configured.

The loop:

    1  rate limiter admits the call
    2  cache is consulted (key covers prompt, schema, model and prompt version)
    3  provider generates, constrained by the JSON schema where it supports one
    4  response is parsed as JSON               -> failure feeds the error back
    5  response is validated against the model  -> failure feeds the error back
    6  extra validators run                     -> failure feeds the error back
    7  every attempt writes a row to llm_calls

Step 6 is how quote verification joins the loop in Phase 3. A quote that is not
a literal substring of the transcript is a validation failure like any other:
the model is told which quote failed and asked to re-extract using exact text.
The brief calls that check "cheap and decisive", and putting it inside the
retry loop rather than after it is what makes the retry useful.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from app.config import Settings, get_settings
from app.db import database
from app.db.repositories import llm_calls as llm_call_repo
from app.errors import LLMError, ProviderUnavailable, RateLimitedError, SchemaValidationError
from app.extraction.llm.base import LLMProvider, LLMRequest, LLMResponse
from app.extraction.llm.cache import ResponseCache, cache_key
from app.extraction.llm.factory import get_llm_provider
from app.extraction.llm.rate_limit import RateLimiter, backoff_seconds
from app.models.telemetry import CallOutcome, LLMCall

logger = logging.getLogger("agent.llm")

T = TypeVar("T", bound=BaseModel)

#: A validator returns None when the value is acceptable, or a message
#: explaining the problem in terms the model can act on.
Validator = Callable[[BaseModel], str | None]

#: Some models wrap JSON in a markdown fence despite being asked not to.
_FENCE = re.compile(r"^\s*```(?:json)?\s*(?P<body>.*?)\s*```\s*$", re.DOTALL)

_limiters: dict[str, RateLimiter] = {}


def _limiter(provider: LLMProvider, settings: Settings) -> RateLimiter:
    """One limiter per provider, shared across calls in this process."""
    if provider.name not in _limiters:
        rpm = settings.gemini_requests_per_minute if provider.name == "gemini" else 0
        _limiters[provider.name] = RateLimiter(rpm)
    return _limiters[provider.name]


def _strip_fence(text: str) -> str:
    match = _FENCE.match(text)
    return match.group("body") if match else text.strip()


def _record(
    settings: Settings,
    *,
    call_id: str,
    # `call_id` groups the attempts of one logical request; each attempt needs
    # its own primary key, or the retries overwrite each other and the retry
    # rate silently reads as zero.
    capability: str,
    provider: LLMProvider,
    attempt: int,
    outcome: CallOutcome,
    response: LLMResponse | None = None,
    source_id: str | None = None,
    prompt_version: str | None = None,
    error: str | None = None,
) -> None:
    """Accounting must never break an extraction run."""
    try:
        with database.transaction(settings) as conn:
            llm_call_repo.record_call(
                conn,
                LLMCall(
                    id=f"{call_id}:{attempt}",
                    call_id=call_id,
                    capability=capability,
                    provider=provider.name,
                    model=provider.model,
                    attempt=attempt,
                    outcome=outcome,
                    source_id=source_id,
                    prompt_version=prompt_version,
                    prompt_tokens=response.prompt_tokens if response else None,
                    completion_tokens=response.completion_tokens if response else None,
                    latency_ms=response.latency_ms if response else None,
                    cache_hit=response.cache_hit if response else False,
                    error=error[:500] if error else None,
                    created_at=datetime.now(timezone.utc).isoformat(),
                ),
            )
    except Exception:  # telemetry is never load-bearing
        # Logged at warning, not debug: a silent swallow here hid a real bug
        # once already, where every retry attempt collided on the same key.
        logger.warning("could not record llm_call for %s", capability, exc_info=True)


def _repair_prompt(original: str, failure: str, previous: str) -> str:
    """Feed the failure back so the next attempt has something to act on.

    Truncated, because a long malformed response pushes the instructions out of
    the model's attention and makes the repair less likely to work.
    """
    return (
        f"{original}\n\n"
        "--- YOUR PREVIOUS RESPONSE WAS REJECTED ---\n"
        f"Problem: {failure}\n\n"
        f"What you returned:\n{previous[:1500]}\n\n"
        "Return only JSON matching the required schema. Fix the problem above. "
        "Every verbatim_quote must be copied exactly from the transcript chunk."
    )


def call_structured(
    capability: str,
    prompt: str,
    response_model: type[T],
    *,
    system: str | None = None,
    source_id: str | None = None,
    prompt_version: str | None = None,
    validators: Sequence[Validator] | None = None,
    settings: Settings | None = None,
    provider: LLMProvider | None = None,
    max_retries: int | None = None,
) -> T:
    """Call the model and return a validated `response_model`.

    Raises `SchemaValidationError` when every attempt failed, carrying the
    reason each one failed so the failure is diagnosable rather than opaque.
    """
    cfg = settings or get_settings()
    llm = provider or get_llm_provider(cfg)
    retries = max_retries if max_retries is not None else cfg.llm_max_retries

    usable, reason = llm.available()
    if not usable:
        raise ProviderUnavailable(reason)

    schema = response_model.model_json_schema()
    cache = ResponseCache(cfg.llm_cache_dir, cfg.llm_cache_enabled)
    limiter = _limiter(llm, cfg)

    call_id = str(uuid.uuid4())
    current_prompt = prompt
    failures: list[str] = []

    for attempt in range(1, retries + 1):
        request = LLMRequest(
            prompt=current_prompt,
            json_schema=schema,
            system=system,
            temperature=cfg.llm_temperature,
        )
        key = cache_key(llm.name, llm.model, request, prompt_version)

        response = cache.get(key)
        if response is None:
            limiter.acquire()
            try:
                response = llm.generate(request)
            except RateLimitedError as exc:
                failures.append(f"attempt {attempt}: rate limited")
                _record(cfg, call_id=call_id, capability=capability, provider=llm, attempt=attempt,
                        outcome=CallOutcome.RATE_LIMITED, source_id=source_id,
                        prompt_version=prompt_version, error=str(exc))
                if attempt == retries:
                    raise
                import time

                time.sleep(backoff_seconds(attempt))
                continue
            except LLMError as exc:
                failures.append(f"attempt {attempt}: {exc}")
                _record(cfg, call_id=call_id, capability=capability, provider=llm, attempt=attempt,
                        outcome=CallOutcome.ERROR, source_id=source_id,
                        prompt_version=prompt_version, error=str(exc))
                if attempt == retries:
                    raise
                continue
            cache.put(key, response)

        raw = _strip_fence(response.text)

        # --- parse ----------------------------------------------------------
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            failure = f"response was not valid JSON: {exc.msg} at line {exc.lineno}"
            failures.append(f"attempt {attempt}: {failure}")
            _record(cfg, call_id=call_id, capability=capability, provider=llm, attempt=attempt,
                    outcome=CallOutcome.PARSE_ERROR, response=response, source_id=source_id,
                    prompt_version=prompt_version, error=failure)
            current_prompt = _repair_prompt(prompt, failure, raw)
            continue

        # --- validate against the schema ------------------------------------
        try:
            value = response_model.model_validate(payload)
        except ValidationError as exc:
            failure = "; ".join(
                f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()[:5]
            )
            failures.append(f"attempt {attempt}: {failure}")
            _record(cfg, call_id=call_id, capability=capability, provider=llm, attempt=attempt,
                    outcome=CallOutcome.VALIDATION_ERROR, response=response, source_id=source_id,
                    prompt_version=prompt_version, error=failure)
            current_prompt = _repair_prompt(prompt, failure, raw)
            continue

        # --- extra validators, including quote verification ------------------
        rejection: str | None = None
        for validator in validators or ():
            rejection = validator(value)
            if rejection:
                break

        if rejection:
            failures.append(f"attempt {attempt}: {rejection}")
            _record(cfg, call_id=call_id, capability=capability, provider=llm, attempt=attempt,
                    outcome=CallOutcome.QUOTE_UNVERIFIED, response=response, source_id=source_id,
                    prompt_version=prompt_version, error=rejection)
            current_prompt = _repair_prompt(prompt, rejection, raw)
            continue

        _record(cfg, call_id=call_id, capability=capability, provider=llm, attempt=attempt,
                outcome=CallOutcome.OK, response=response, source_id=source_id,
                prompt_version=prompt_version)
        return value

    raise SchemaValidationError(
        f"{capability}: the model produced no valid response in {retries} attempts. "
        + " | ".join(failures)
    )
