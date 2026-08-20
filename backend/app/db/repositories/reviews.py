"""Reads and writes over `review_events` (M6).

Append-only, enforced by trigger. "Who approved what, when, and what did they
change" is answerable from this table alone, including for records that were
later edited.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from enum import StrEnum

from app.models.common import ReviewStatus, StrictModel

_COLUMNS = (
    "id, extraction_id, event_type, actor, status_before, status_after, "
    "payload_before, payload_after, note, created_at"
)


class ReviewEventType(StrEnum):
    CREATED = "created"
    EDITED = "edited"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class ReviewEvent(StrictModel):
    id: str
    extraction_id: str
    event_type: ReviewEventType
    actor: str
    status_before: ReviewStatus | None = None
    status_after: ReviewStatus | None = None
    payload_before: dict | None = None
    payload_after: dict | None = None
    note: str | None = None
    created_at: str


def record(
    conn: sqlite3.Connection,
    extraction_id: str,
    event_type: ReviewEventType,
    actor: str,
    *,
    status_before: ReviewStatus | None = None,
    status_after: ReviewStatus | None = None,
    payload_before: dict | None = None,
    payload_after: dict | None = None,
    note: str | None = None,
) -> ReviewEvent:
    event = ReviewEvent(
        id=str(uuid.uuid4()),
        extraction_id=extraction_id,
        event_type=event_type,
        actor=actor,
        status_before=status_before,
        status_after=status_after,
        payload_before=payload_before,
        payload_after=payload_after,
        note=note,
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    conn.execute(
        f"INSERT INTO review_events ({_COLUMNS}) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            event.id,
            event.extraction_id,
            event.event_type.value,
            event.actor,
            status_before.value if status_before else None,
            status_after.value if status_after else None,
            json.dumps(payload_before) if payload_before is not None else None,
            json.dumps(payload_after) if payload_after is not None else None,
            event.note,
            event.created_at,
        ),
    )
    return event


def _row(row: sqlite3.Row) -> ReviewEvent:
    return ReviewEvent(
        id=row["id"],
        extraction_id=row["extraction_id"],
        event_type=ReviewEventType(row["event_type"]),
        actor=row["actor"],
        status_before=ReviewStatus(row["status_before"]) if row["status_before"] else None,
        status_after=ReviewStatus(row["status_after"]) if row["status_after"] else None,
        payload_before=json.loads(row["payload_before"]) if row["payload_before"] else None,
        payload_after=json.loads(row["payload_after"]) if row["payload_after"] else None,
        note=row["note"],
        created_at=row["created_at"],
    )


def history(conn: sqlite3.Connection, extraction_id: str) -> list[ReviewEvent]:
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM review_events WHERE extraction_id = ? ORDER BY created_at",
        (extraction_id,),
    ).fetchall()
    return [_row(row) for row in rows]


def recent(conn: sqlite3.Connection, limit: int = 100) -> list[ReviewEvent]:
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM review_events ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()
    return [_row(row) for row in rows]


def by_actor(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT actor, COUNT(*) AS n FROM review_events"
        " WHERE event_type IN ('approved','rejected') GROUP BY actor"
    ).fetchall()
    return {row["actor"]: row["n"] for row in rows}
