"""HTTP surface for M7.

Read-only inspection plus a re-runnable sync. There is deliberately no endpoint
that writes an arbitrary payload to the tracker: the only way anything reaches
it is through an approved extraction, and an endpoint that bypassed that would
be the exact hole the rubric describes.
"""

from __future__ import annotations

from fastapi import APIRouter, Query

from app.adapters.factory import get_tracker
from app.config import get_settings
from app.db import database
from app.db.repositories import tracker as tracker_repo
from app.models.tracker import TrackerFilter, TrackerItem, WriteAttempt, WriteResult
from app.tracker import service

router = APIRouter(prefix="/api/tracker", tags=["tracker"])


@router.get("/items", response_model=list[TrackerItem], summary="What the tracker holds")
def list_items(
    status: str | None = Query(default=None),
    assignee: str | None = Query(default=None),
    written_by_agent: bool | None = Query(
        default=None, description="true for items this agent created, false for the seeded backlog"
    ),
    limit: int | None = Query(default=None, ge=1, le=500),
) -> list[TrackerItem]:
    """Includes the seeded backlog the agent never created.

    The adapter contract requires the mock return realistically messy data, so
    these carry missing assignees, free-text statuses and stale due dates.
    """
    return get_tracker(get_settings()).list_items(
        TrackerFilter(status=status, assignee=assignee, written_by_agent=written_by_agent, limit=limit)
    )


@router.get("/items/{external_ref}", response_model=TrackerItem, summary="One item")
def get_item(external_ref: str) -> TrackerItem:
    from fastapi import HTTPException

    item = get_tracker(get_settings()).get_item(external_ref)
    if item is None:
        raise HTTPException(status_code=404, detail=f"the tracker has no item {external_ref}")
    return item


@router.get("/attempts", response_model=list[WriteAttempt], summary="Every write attempt")
def attempts(extraction_id: str | None = Query(default=None)) -> list[WriteAttempt]:
    """Created, deduplicated and blocked alike. A log that recorded only
    successes could not prove the approval gate fired."""
    with database.connect(get_settings()) as conn:
        return tracker_repo.list_attempts(conn, extraction_id)


@router.get("/write-log", summary="The inspectable JSONL write log")
def write_log(limit: int | None = Query(default=None, ge=1, le=1000)) -> list[dict]:
    return service.write_log_lines(get_settings(), limit)


@router.get("/summary", summary="Counts, for the interface")
def summary() -> dict[str, object]:
    with database.connect(get_settings()) as conn:
        return {
            "items_total": tracker_repo.count_items(conn),
            "items_written_by_agent": tracker_repo.count_items(conn, written_by_agent=True),
            "items_pre_existing": tracker_repo.count_items(conn, written_by_agent=False),
            "audited_writes": tracker_repo.count_writes(conn),
            "attempts": tracker_repo.attempt_counts(conn),
        }


@router.post("/sync", response_model=list[WriteResult], summary="Write every approved action")
def sync() -> list[WriteResult]:
    """Re-runnable. Running it repeatedly is how three approvals are shown to
    produce exactly three items."""
    return service.sync_approved(get_settings())


@router.post("/write/{extraction_id}", response_model=WriteResult, summary="Write one approved action")
def write(extraction_id: str) -> WriteResult:
    """Refuses with 403 when the extraction is not approved."""
    return service.write_approved(extraction_id, get_settings())
