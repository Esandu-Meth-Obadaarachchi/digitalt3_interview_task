"""HTTP surface for M6, the review and approval queue.

The gate is not here. Every rule lives in `app.review.queue` and in the
database, so calling this API directly is no different from using the UI.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Query

from app.config import get_settings
from app.db.repositories.reviews import ReviewEvent
from app.models.common import ExtractionType, ReviewStatus, StrictModel
from app.models.extraction import Extraction
from app.review import queue

router = APIRouter(prefix="/api/review", tags=["review"])


class EditRequest(StrictModel):
    payload: dict
    reviewer: str
    note: str | None = None


class DecisionRequest(StrictModel):
    reviewer: str
    note: str | None = None
    override_unverified: bool = False


@router.get("", response_model=list[Extraction], summary="The queue")
def list_queue(
    status: ReviewStatus | None = Query(default=ReviewStatus.PENDING),
    source_id: str | None = Query(default=None),
    extraction_type: ExtractionType | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1, le=500),
) -> list[Extraction]:
    return queue.list_queue(
        get_settings(), status=status, source_id=source_id, extraction_type=extraction_type, limit=limit
    )


@router.get("/summary", response_model=queue.QueueSummary, summary="Counts by state")
def queue_summary(source_id: str | None = Query(default=None)) -> queue.QueueSummary:
    return queue.summary(get_settings(), source_id)


@router.get("/{extraction_id}/history", response_model=list[ReviewEvent], summary="Audit trail")
def history(extraction_id: str) -> list[ReviewEvent]:
    """Append-only. Who did what, when, and what the payload looked like before."""
    return queue.history(extraction_id, get_settings())


@router.post("/{extraction_id}/edit", response_model=Extraction, summary="Correct a payload")
def edit(extraction_id: str, request: EditRequest) -> Extraction:
    """The item stays pending. An edit is not an approval."""
    return queue.edit(extraction_id, request.payload, request.reviewer, request.note, get_settings())


@router.post("/{extraction_id}/approve", response_model=Extraction, summary="Approve")
def approve(extraction_id: str, request: DecisionRequest) -> Extraction:
    """Refuses with 409 when the quote was never verified, unless the caller
    sets override_unverified and gives a written reason."""
    return queue.approve(
        extraction_id,
        request.reviewer,
        request.note,
        override_unverified=request.override_unverified,
        settings=get_settings(),
    )


@router.post("/{extraction_id}/reject", response_model=Extraction, summary="Reject")
def reject(extraction_id: str, request: DecisionRequest) -> Extraction:
    return queue.reject(extraction_id, request.reviewer, request.note, get_settings())


@router.post("/expire", response_model=list[str], summary="Sweep unreviewed items")
def expire(now: datetime | None = Query(default=None, description="clock override for the demo")) -> list[str]:
    """The safe default on no response. Expired items are never writable."""
    return queue.expire_stale(get_settings(), now)
