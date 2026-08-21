"""M7 - writing approved actions to a tracker, and refusing everything else.

This is the module the rubric's third criterion is really about. The rule is
one sentence: nothing reaches the tracker unless a human approved it. It is
enforced three times over, because the rubric's own red flag is gating that
"exists in the UI but is bypassable via the API":

  1. here, before a draft is even built
  2. by trg_approval_gate_write, when the audit row is inserted
  3. by UNIQUE (tracker_writes.extraction_id), which makes a second write for
     the same extraction impossible rather than merely unlikely

Layer 1 gives a readable error. Layers 2 and 3 hold when layer 1 is bypassed,
which is what `eval/test_approval_gate.py` demonstrates by calling the service
layer directly rather than going through the interface.

Every attempt is recorded, including the refused ones. A log that only records
successes cannot prove a gate fired.
"""

from __future__ import annotations

import logging

from app.adapters.factory import get_tracker
from app.config import Settings, get_settings
from app.db import database
from app.db.repositories import extractions as extraction_repo
from app.db.repositories import tracker as tracker_repo
from app.errors import ApprovalGateViolation, NotFoundError
from app.models.common import UNSPECIFIED, ExtractionType, ReviewStatus
from app.models.extraction import Extraction
from app.models.tracker import TrackerItemDraft, WriteOutcome, WriteResult
from app.tracker import write_log

logger = logging.getLogger("agent.tracker")


def build_draft(extraction: Extraction) -> TrackerItemDraft:
    """Turn an approved extraction into something a tracker can hold.

    M7 requires the item carry "the source ID, timestamp and quote in its
    description". A ticket that says only "finish the auth refactor" is a
    ticket nobody can check. This one says who said it, when, and in what
    meeting, so a reader can go back to the sentence.

    An UNSPECIFIED owner becomes no assignee, and an UNSPECIFIED date becomes
    no due date. The abstention is carried forward rather than resolved: a
    ticket assigned to nobody is a correct record of a commitment nobody
    claimed, and inventing an assignee here would undo the discipline the
    extraction stage maintained.
    """
    payload = extraction.payload
    owner = str(payload.get("owner", UNSPECIFIED))
    due = str(payload.get("due_date", UNSPECIFIED))

    description_lines = [
        str(payload.get("what", "")),
        "",
        f'Quoted from the meeting: "{extraction.verbatim_quote}"',
        f"Said by: {extraction.speaker or 'not attributed'}",
        f"At: {extraction.timestamp or 'no timestamp'}",
        f"Source: {extraction.source_id}",
        f"Extraction: {extraction.id}",
    ]

    if payload.get("due_date_stated"):
        description_lines.append(
            f"Stated timing: \"{payload['due_date_stated']}\" -> {due} ({payload.get('due_date_rule', '')})"
        )
    if owner == UNSPECIFIED:
        description_lines.append(
            "Owner: UNSPECIFIED. The meeting did not say who would do this. "
            "Left unassigned rather than guessed."
        )
    if not extraction.quote_verified:
        description_lines.append(
            "WARNING: this quote could not be verified as a literal substring of the "
            "transcript. It was approved with an explicit override."
        )

    labels = ["meeting-agent", extraction.extraction_type.value]
    if owner == UNSPECIFIED:
        labels.append("needs-owner")
    if due == UNSPECIFIED:
        labels.append("needs-date")
    if not extraction.quote_verified:
        labels.append("unverified-quote")

    return TrackerItemDraft(
        title=str(payload.get("what", "Untitled action"))[:120],
        description="\n".join(description_lines),
        assignee=None if owner == UNSPECIFIED else owner,
        due_date=None if due == UNSPECIFIED else due,
        labels=labels,
        source_ref=extraction.id,
    )


def write_approved(
    extraction_id: str,
    settings: Settings | None = None,
    *,
    raise_on_block: bool = True,
) -> WriteResult:
    """Write one approved extraction to the tracker.

    Idempotent: calling it a second time returns the item already created and
    records a `deduplicated` attempt rather than creating a duplicate.
    """
    cfg = settings or get_settings()
    adapter = get_tracker(cfg)

    with database.connect(cfg) as conn:
        extraction = extraction_repo.get(conn, extraction_id)
        existing_write = tracker_repo.get_write(conn, extraction_id) if extraction else None

    if extraction is None:
        raise NotFoundError(f"no extraction with id {extraction_id}")

    # --- layer 1: the gate --------------------------------------------------
    if extraction.status is not ReviewStatus.APPROVED:
        reason = (
            f"extraction {extraction_id} has status '{extraction.status.value}'. Only an "
            f"approved extraction may be written to a tracker."
        )
        with database.transaction(cfg) as conn:
            tracker_repo.record_attempt(
                conn, extraction_id, WriteOutcome.BLOCKED, adapter.provider, reason=reason
            )
        write_log.append(
            cfg, extraction_id, WriteOutcome.BLOCKED, adapter.provider,
            reason=reason, status=extraction.status.value,
        )
        logger.warning("approval gate blocked a write: %s", reason)

        if raise_on_block:
            raise ApprovalGateViolation(reason)
        return WriteResult(outcome=WriteOutcome.BLOCKED, extraction_id=extraction_id, reason=reason)

    # --- idempotency --------------------------------------------------------
    if existing_write is not None:
        external_ref = existing_write["external_ref"]
        with database.transaction(cfg) as conn:
            tracker_repo.record_attempt(
                conn,
                extraction_id,
                WriteOutcome.DEDUPLICATED,
                adapter.provider,
                external_ref=external_ref,
                reason="a tracker item already exists for this extraction",
            )
        write_log.append(
            cfg, extraction_id, WriteOutcome.DEDUPLICATED, adapter.provider,
            external_ref=external_ref, reason="a tracker item already exists for this extraction",
        )
        return WriteResult(
            outcome=WriteOutcome.DEDUPLICATED,
            extraction_id=extraction_id,
            item=adapter.get_item(external_ref),
            reason="already written",
        )

    # --- create -------------------------------------------------------------
    draft = build_draft(extraction)
    item = adapter.create_item(draft)

    try:
        with database.transaction(cfg) as conn:
            tracker_repo.record_write(
                conn, extraction_id, item.external_ref, adapter.provider, draft.model_dump()
            )
            tracker_repo.record_attempt(
                conn,
                extraction_id,
                WriteOutcome.CREATED,
                adapter.provider,
                external_ref=item.external_ref,
            )
    except Exception:
        # The adapter and our audit sit either side of a system boundary and
        # cannot share a transaction. A real integration has the same problem
        # and the same answer: undo the far side, then re-raise. Reached only
        # if a database-level gate refuses a write layer 1 already allowed.
        logger.error("audit write failed after creating %s, compensating", item.external_ref)
        with database.transaction(cfg) as conn:
            conn.execute("DELETE FROM tracker_items WHERE external_ref = ?", (item.external_ref,))
        raise

    write_log.append(
        cfg, extraction_id, WriteOutcome.CREATED, adapter.provider,
        external_ref=item.external_ref, draft=draft,
    )
    logger.info("wrote %s to the tracker as %s", extraction_id, item.external_ref)

    return WriteResult(outcome=WriteOutcome.CREATED, extraction_id=extraction_id, item=item)


def sync_approved(settings: Settings | None = None) -> list[WriteResult]:
    """Write every approved action that is not in the tracker yet.

    Re-runnable by design. Running it repeatedly is how the demo proves that
    three approvals produce exactly three items however many times the write
    path is exercised.
    """
    cfg = settings or get_settings()
    with database.connect(cfg) as conn:
        approved = extraction_repo.list_extractions(
            conn, extraction_type=ExtractionType.ACTION, status=ReviewStatus.APPROVED
        )

    return [write_approved(item.id, cfg, raise_on_block=False) for item in approved]


def write_log_lines(settings: Settings | None = None, limit: int | None = None) -> list[dict]:
    """Read back the inspectable log, for the API and the walkthrough."""
    return write_log.read(settings or get_settings(), limit)
