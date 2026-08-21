"""M5 - extracting risks and blockers from a transcript.

The pipeline is shared with M3 and M4. What is specific to risks is the prompt,
the contract, and one deduplication choice.

`owner_of` returns UNSPECIFIED unconditionally, which disables the cross-region
deduplication rule for risks. That rule merges the same named person committing
to the same thing in two places, and it is right for actions and decisions. It
is wrong here: raising a risk is not owning it, so the same person flagging two
related concerns in different parts of a meeting is two risks, not one stated
twice. Risks fall back to the within-region rule, which needs overlapping quote
spans and so cannot merge across a meeting.
"""

from __future__ import annotations

from app.config import Settings
from app.extraction.pipeline import ExtractionRun, ExtractionSpec, run_extraction
from app.models.common import UNSPECIFIED, ExtractionType
from app.models.extraction import DraftRisk, DraftRiskList
from app.models.source import Source

PROMPT_NAME = "extract_risks"


def risk_payload(item: DraftRisk, source: Source) -> dict:
    return {
        "description": item.description,
        "severity": item.severity.value,
        "affected_area": item.affected_area,
        "owner": item.owner,
    }


RISK_SPEC = ExtractionSpec(
    extraction_type=ExtractionType.RISK,
    prompt_name=PROMPT_NAME,
    response_model=DraftRiskList,
    items_field="risks",
    task_of=lambda item: item.description,
    # Deliberately not item.owner. See the module docstring.
    owner_of=lambda item: UNSPECIFIED,
    payload_of=risk_payload,
)


def extract_risks(
    source_id: str,
    settings: Settings | None = None,
    *,
    replace_pending: bool = True,
) -> ExtractionRun:
    """Run M5 over one ingested source and leave the results in the queue."""
    return run_extraction(source_id, RISK_SPEC, settings, replace_pending=replace_pending)
