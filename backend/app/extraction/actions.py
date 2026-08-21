"""M3 - extracting action items from a transcript.

The pipeline itself lives in `pipeline.py` and is shared with M4, M5 and M9.
What is specific to actions is here: the prompt to load, the contract the model
must satisfy, and the date resolution that turns what a person said into a
concrete due date without inventing one.
"""

from __future__ import annotations

from app.config import Settings
from app.extraction.dates import resolve_due_date
from app.extraction.pipeline import ExtractionRun, ExtractionSpec, run_extraction
from app.models.common import ExtractionType
from app.models.extraction import DraftAction, DraftActionList
from app.models.source import Source

PROMPT_NAME = "extract_actions"


def action_payload(item: DraftAction, source: Source) -> dict:
    """Store the resolved date, and everything needed to defend it.

    `due_date` is what downstream consumers read: an ISO date or UNSPECIFIED.
    The three fields beside it exist because golden case 4 measures invented
    dates, and a resolved date is otherwise indistinguishable from one the
    transcript stated.
    """
    due = resolve_due_date(item.due_date, source.meeting_date)
    return {
        "what": item.what,
        "owner": item.owner,
        "due_date": due.value,
        "due_date_type": due.date_type.value,
        "due_date_stated": due.stated_text,
        "due_date_rule": due.rule,
    }


ACTION_SPEC = ExtractionSpec(
    extraction_type=ExtractionType.ACTION,
    prompt_name=PROMPT_NAME,
    response_model=DraftActionList,
    items_field="actions",
    task_of=lambda item: item.what,
    owner_of=lambda item: item.owner,
    payload_of=action_payload,
)


def extract_actions(
    source_id: str,
    settings: Settings | None = None,
    *,
    replace_pending: bool = True,
) -> ExtractionRun:
    """Run M3 over one ingested source and leave the results in the queue."""
    return run_extraction(source_id, ACTION_SPEC, settings, replace_pending=replace_pending)
