"""M6 - the review and approval queue.

"Approval must be enforced in the data model, not only in the interface."

This module holds the rules. The API calls it, the UI calls the API, and the
database enforces the same constraints underneath, so an approval cannot be
manufactured by talking to a lower layer.

Three rules live here that the database alone cannot express:

  1. An unverified quote cannot be approved without an explicit override and a
     written reason. The database will accept the row; a distracted click
     should not.
  2. Every transition writes an append-only audit event, including what the
     payload looked like before and after.
  3. An unreviewed item ages out to `expired`, which the approval-gate trigger
     treats exactly like `pending`: not writable. The safe default on no
     response is refusal, never implicit approval.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.config import Settings, get_settings
from app.db import database
from app.db.repositories import extractions as extraction_repo
from app.db.repositories import reviews as review_repo
from app.errors import NotFoundError, ReviewStateError
from app.models.common import ExtractionType, ReviewStatus, StrictModel
from app.models.extraction import Extraction

logger = logging.getLogger("agent.review")


class QueueSummary(StrictModel):
    pending: int = 0
    approved: int = 0
    rejected: int = 0
    expired: int = 0
    unverified_pending: int = 0

    @property
    def total(self) -> int:
        return self.pending + self.approved + self.rejected + self.expired


def _load(conn, extraction_id: str) -> Extraction:
    extraction = extraction_repo.get(conn, extraction_id)
    if extraction is None:
        raise NotFoundError(f"no extraction with id {extraction_id}")
    return extraction


def _require_pending(extraction: Extraction, verb: str) -> None:
    if extraction.status is not ReviewStatus.PENDING:
        raise ReviewStateError(
            f"cannot {verb} extraction {extraction.id}: its status is "
            f"'{extraction.status.value}', and only a pending item can be reviewed. "
            f"Approved, rejected and expired are terminal states."
        )


def list_queue(
    settings: Settings | None = None,
    *,
    status: ReviewStatus | None = ReviewStatus.PENDING,
    source_id: str | None = None,
    extraction_type: ExtractionType | None = None,
    limit: int | None = None,
) -> list[Extraction]:
    """Unverified quotes sort first: they are the ones a reviewer must look at."""
    with database.connect(settings or get_settings()) as conn:
        return extraction_repo.list_extractions(
            conn,
            source_id=source_id,
            extraction_type=extraction_type,
            status=status,
            limit=limit,
        )


def summary(settings: Settings | None = None, source_id: str | None = None) -> QueueSummary:
    with database.connect(settings or get_settings()) as conn:
        counts = extraction_repo.counts_by_status(conn, source_id)
        unverified = len(
            extraction_repo.list_extractions(
                conn, source_id=source_id, status=ReviewStatus.PENDING, quote_verified=False
            )
        )
    return QueueSummary(
        pending=counts.get("pending", 0),
        approved=counts.get("approved", 0),
        rejected=counts.get("rejected", 0),
        expired=counts.get("expired", 0),
        unverified_pending=unverified,
    )


def edit(
    extraction_id: str,
    payload: dict,
    actor: str,
    note: str | None = None,
    settings: Settings | None = None,
) -> Extraction:
    """Apply a human correction. The item stays pending and must still be approved.

    `original_payload` is immutable by trigger, so the model's first output
    survives every edit and the review surface can always show both.
    """
    cfg = settings or get_settings()
    with database.transaction(cfg) as conn:
        extraction = _load(conn, extraction_id)
        _require_pending(extraction, "edit")

        extraction_repo.update_payload(conn, extraction_id, payload, extraction.verbatim_quote)
        review_repo.record(
            conn,
            extraction_id,
            review_repo.ReviewEventType.EDITED,
            actor,
            status_before=extraction.status,
            status_after=extraction.status,
            payload_before=extraction.payload,
            payload_after=payload,
            note=note,
        )
        return _load(conn, extraction_id)


def approve(
    extraction_id: str,
    actor: str,
    note: str | None = None,
    *,
    override_unverified: bool = False,
    settings: Settings | None = None,
) -> Extraction:
    """Approve an item, which is the only way it becomes writable downstream."""
    if not actor or not actor.strip():
        raise ReviewStateError("approval requires the name of the person approving")

    cfg = settings or get_settings()
    with database.transaction(cfg) as conn:
        extraction = _load(conn, extraction_id)
        _require_pending(extraction, "approve")

        if extraction.needs_override_to_approve and not override_unverified:
            raise ReviewStateError(
                f"extraction {extraction_id} carries a quote that could not be verified as a "
                f"literal substring of the transcript. Approving it requires an explicit "
                f"override and a written reason, so that an unverifiable claim is never "
                f"waved through by accident."
            )
        if extraction.needs_override_to_approve and not (note or "").strip():
            raise ReviewStateError(
                "overriding quote verification requires a written reason, which is kept in "
                "the audit trail"
            )

        extraction_repo.set_status(conn, extraction_id, ReviewStatus.APPROVED, actor, note)
        review_repo.record(
            conn,
            extraction_id,
            review_repo.ReviewEventType.APPROVED,
            actor,
            status_before=extraction.status,
            status_after=ReviewStatus.APPROVED,
            payload_before=extraction.payload,
            payload_after=extraction.payload,
            note=(
                f"OVERRIDE of unverified quote: {note}"
                if extraction.needs_override_to_approve
                else note
            ),
        )
        logger.info("%s approved %s", actor, extraction_id)
        return _load(conn, extraction_id)


def reject(
    extraction_id: str,
    actor: str,
    note: str | None = None,
    settings: Settings | None = None,
) -> Extraction:
    if not actor or not actor.strip():
        raise ReviewStateError("rejection requires the name of the person rejecting")

    cfg = settings or get_settings()
    with database.transaction(cfg) as conn:
        extraction = _load(conn, extraction_id)
        _require_pending(extraction, "reject")

        extraction_repo.set_status(conn, extraction_id, ReviewStatus.REJECTED, actor, note)
        review_repo.record(
            conn,
            extraction_id,
            review_repo.ReviewEventType.REJECTED,
            actor,
            status_before=extraction.status,
            status_after=ReviewStatus.REJECTED,
            payload_before=extraction.payload,
            note=note,
        )
        logger.info("%s rejected %s", actor, extraction_id)
        return _load(conn, extraction_id)


def expire_stale(settings: Settings | None = None, now: datetime | None = None) -> list[str]:
    """The safe default for an item nobody reviewed.

    `now` is injectable so the behaviour is demonstrable without waiting three
    days, which is the same clock-override principle the scheduled digest uses.
    """
    cfg = settings or get_settings()
    moment = (now or datetime.now(timezone.utc)).isoformat()
    expired: list[str] = []

    with database.transaction(cfg) as conn:
        rows = conn.execute(
            "SELECT id FROM extractions WHERE status = 'pending'"
            " AND expires_at IS NOT NULL AND expires_at <= ?",
            (moment,),
        ).fetchall()

        for row in rows:
            extraction = _load(conn, row["id"])
            conn.execute("UPDATE extractions SET status = 'expired' WHERE id = ?", (row["id"],))
            review_repo.record(
                conn,
                row["id"],
                review_repo.ReviewEventType.EXPIRED,
                "system",
                status_before=ReviewStatus.PENDING,
                status_after=ReviewStatus.EXPIRED,
                payload_before=extraction.payload,
                note=(
                    f"no review within {cfg.pending_expiry_hours} hours. Expired items are "
                    f"never written downstream: the safe default is refusal."
                ),
            )
            expired.append(row["id"])

    if expired:
        logger.info("expired %s unreviewed extraction(s)", len(expired))
    return expired


def history(extraction_id: str, settings: Settings | None = None) -> list[review_repo.ReviewEvent]:
    with database.connect(settings or get_settings()) as conn:
        return review_repo.history(conn, extraction_id)
