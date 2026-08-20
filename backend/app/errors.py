"""Domain errors.

The database triggers raise `sqlite3.IntegrityError` with a prefixed message
("consent_gate: ...", "approval_gate: ..."). `translate_sqlite_error` turns
those into named Python exceptions so the API returns a meaningful status code
and the test suite asserts on a type rather than on a substring of a message.
"""

from __future__ import annotations

import sqlite3


class AgentError(Exception):
    """Base class for every error this application raises deliberately."""

    status_code: int = 400
    code: str = "agent_error"


class ConsentRefused(AgentError):
    """M2. The source did not give consent, so nothing may be processed."""

    status_code = 403
    code = "consent_refused"


class ApprovalGateViolation(AgentError):
    """M6. A write was attempted for an extraction that is not approved."""

    status_code = 403
    code = "approval_gate_violation"


class ReviewStateError(AgentError):
    """M6. An illegal review transition, such as reopening a rejected item."""

    status_code = 409
    code = "review_state_error"


class AuditViolation(AgentError):
    """An attempt to mutate an append-only or immutable audit record."""

    status_code = 403
    code = "audit_violation"


class DuplicateWrite(AgentError):
    """M7. A tracker item already exists for this extraction."""

    status_code = 409
    code = "duplicate_write"


class IngestionError(AgentError):
    """M1. A source could not be parsed into segments."""

    status_code = 422
    code = "ingestion_error"


class MalformedSourceError(IngestionError):
    """M1. The file was detected as truncated, unlabelled or badly encoded."""

    code = "malformed_source"


class NotFoundError(AgentError):
    status_code = 404
    code = "not_found"


class LLMError(AgentError):
    """The model failed after every retry, or the provider is unreachable."""

    status_code = 502
    code = "llm_error"


class SchemaValidationError(LLMError):
    """The model returned output that never validated against the schema."""

    code = "schema_validation_error"


class RateLimitedError(LLMError):
    status_code = 429
    code = "rate_limited"


class ProviderUnavailable(LLMError):
    status_code = 503
    code = "provider_unavailable"


# Message prefix emitted by each trigger -> the exception it becomes.
_TRIGGER_PREFIXES: dict[str, type[AgentError]] = {
    "consent_gate:": ConsentRefused,
    "approval_gate:": ApprovalGateViolation,
    "review_state:": ReviewStateError,
    "review_audit:": ReviewStateError,
    "audit:": AuditViolation,
    "sources:": IngestionError,
}


def translate_sqlite_error(exc: sqlite3.Error) -> Exception:
    """Map a database-level refusal onto a domain error.

    Anything unrecognised is returned unchanged, so a genuine bug surfaces as
    a genuine bug rather than being swallowed by a friendly message.
    """
    message = str(exc)

    for prefix, error_type in _TRIGGER_PREFIXES.items():
        if message.startswith(prefix):
            return error_type(message[len(prefix) :].strip())

    if "UNIQUE constraint failed: tracker_writes.extraction_id" in message:
        return DuplicateWrite("a tracker item already exists for this extraction")

    if "CHECK constraint failed: is_direct_message" in message:
        return AuditViolation("direct messages are excluded by construction")

    return exc
