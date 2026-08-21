"""M4 - extracting decisions from a transcript.

The pipeline is shared with M3 and M5. What is specific to decisions is the
prompt, the contract, and one deduplication choice worth explaining.

`owner_of` returns who stated the decision. That feeds the cross-region
deduplication rule, which merges the same named person settling the same
question in two places. For a decision that is precisely the closing-recap
case: a meeting that ends "so the decision is single-tenant for Phase 1" is
restating what was settled at 00:06:58, not deciding it again.
"""

from __future__ import annotations

from app.config import Settings
from app.extraction.pipeline import ExtractionRun, ExtractionSpec, run_extraction
from app.models.common import ExtractionType
from app.models.extraction import DraftDecision, DraftDecisionList
from app.models.source import Source

PROMPT_NAME = "extract_decisions"


def decision_payload(item: DraftDecision, source: Source) -> dict:
    return {
        "what_was_decided": item.what_was_decided,
        "stated_rationale": item.stated_rationale,
        "who_stated_it": item.who_stated_it,
        "alternatives_discussed": item.alternatives_discussed,
    }


DECISION_SPEC = ExtractionSpec(
    extraction_type=ExtractionType.DECISION,
    prompt_name=PROMPT_NAME,
    response_model=DraftDecisionList,
    items_field="decisions",
    task_of=lambda item: item.what_was_decided,
    owner_of=lambda item: item.who_stated_it,
    payload_of=decision_payload,
)


def extract_decisions(
    source_id: str,
    settings: Settings | None = None,
    *,
    replace_pending: bool = True,
) -> ExtractionRun:
    """Run M4 over one ingested source and leave the results in the queue."""
    return run_extraction(source_id, DECISION_SPEC, settings, replace_pending=replace_pending)
