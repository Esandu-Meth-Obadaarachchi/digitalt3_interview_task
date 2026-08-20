"""M2 - the consent gate.

The brief: "Refuse to process any source whose consent flag is not explicitly
true", and the capability test requires the non-consented meeting is "never
transcribed, never sent to a model, and produces zero extracted items".

So the gate fires on metadata alone, before the file is opened. Nothing is
read, nothing is parsed, nothing is stored beyond a refusal record saying why.
`bytes_read` on the resulting report stays at zero, which is the evidence that
the content was never touched.

This is one of three layers:
  1. here, on metadata, before any file access
  2. in the extraction service, before any model call
  3. in the database, via trg_consent_gate_insert
Any one of them alone would satisfy the letter of the requirement. Together
they mean no code path reaches an extraction for a non-consented source.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.errors import ConsentRefused
from app.models.ingestion import ConsentDecision
from app.models.source import SourceMetadata


def evaluate_consent(metadata: SourceMetadata) -> ConsentDecision:
    """Decide, without touching the file.

    `consent_flag` is a required field on SourceMetadata with no default, so a
    source whose metadata omits it fails validation before reaching here. That
    is deliberate: absent consent is not consent, and a default in either
    direction would be a decision the source did not make.
    """
    now = datetime.now(timezone.utc).isoformat()

    if metadata.consent_flag is True:
        return ConsentDecision(
            source_id=metadata.id,
            granted=True,
            reason="consent_flag is true in the source metadata",
            checked_at=now,
        )

    return ConsentDecision(
        source_id=metadata.id,
        granted=False,
        reason=(
            "consent_flag is not true in the source metadata. The file was not opened, "
            "not parsed, not transcribed and not sent to any model."
        ),
        checked_at=now,
    )


def require_consent(metadata: SourceMetadata) -> ConsentDecision:
    """Raise `ConsentRefused` unless consent was explicitly granted."""
    decision = evaluate_consent(metadata)
    if decision.refused:
        raise ConsentRefused(decision.reason)
    return decision
