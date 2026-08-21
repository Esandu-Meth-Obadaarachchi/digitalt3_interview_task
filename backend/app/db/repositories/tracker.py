"""Reads and writes over `tracker_items`, `tracker_writes` and
`tracker_write_attempts` (M7).

Three tables, three different questions:

  tracker_items           what the tracker holds, including the seeded backlog
                          the agent never created
  tracker_writes          our audit of what we put there, one row per approved
                          extraction, UNIQUE on extraction_id
  tracker_write_attempts  every attempt, including deduplicated and blocked
"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone

from app.models.tracker import TrackerFilter, TrackerItem, WriteAttempt, WriteOutcome

_ITEM_COLUMNS = (
    "external_ref, title, description, assignee, status, due_date, labels, "
    "source_ref, seeded, created_at, updated_at"
)
_REF_PATTERN = re.compile(r"^MOCK-(\d+)$")


# --- tracker_items -----------------------------------------------------------


def row_to_item(row: sqlite3.Row) -> TrackerItem:
    return TrackerItem(
        external_ref=row["external_ref"],
        title=row["title"],
        description=row["description"],
        assignee=row["assignee"],
        status=row["status"],
        due_date=row["due_date"],
        labels=json.loads(row["labels"] or "[]"),
        source_ref=row["source_ref"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def next_reference(conn: sqlite3.Connection) -> str:
    """The next free MOCK-n, counting past the seeded backlog.

    Derived from the highest existing number rather than from a row count, so
    it stays correct when the seeded items are not contiguous.
    """
    highest = 0
    for row in conn.execute("SELECT external_ref FROM tracker_items"):
        match = _REF_PATTERN.match(row["external_ref"])
        if match:
            highest = max(highest, int(match.group(1)))
    return f"MOCK-{highest + 1}"


def insert_item(conn: sqlite3.Connection, item: TrackerItem, *, seeded: bool) -> TrackerItem:
    conn.execute(
        f"INSERT INTO tracker_items ({_ITEM_COLUMNS}) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            item.external_ref,
            item.title,
            item.description,
            item.assignee,
            item.status,
            item.due_date,
            json.dumps(item.labels),
            item.source_ref,
            int(seeded),
            item.created_at,
            item.updated_at,
        ),
    )
    return item


def get_item(conn: sqlite3.Connection, external_ref: str) -> TrackerItem | None:
    row = conn.execute(
        f"SELECT {_ITEM_COLUMNS} FROM tracker_items WHERE external_ref = ?", (external_ref,)
    ).fetchone()
    return row_to_item(row) if row else None


def get_item_by_source(conn: sqlite3.Connection, source_ref: str) -> TrackerItem | None:
    row = conn.execute(
        f"SELECT {_ITEM_COLUMNS} FROM tracker_items WHERE source_ref = ?", (source_ref,)
    ).fetchone()
    return row_to_item(row) if row else None


def list_items(conn: sqlite3.Connection, criteria: TrackerFilter) -> list[TrackerItem]:
    clauses, params = [], []
    if criteria.status is not None:
        # Trailing whitespace and inconsistent casing are normal in a real
        # backlog, so a status filter compares the trimmed, lowered form.
        clauses.append("LOWER(TRIM(status)) = LOWER(TRIM(?))")
        params.append(criteria.status)
    if criteria.assignee is not None:
        clauses.append("assignee = ?")
        params.append(criteria.assignee)
    if criteria.source_ref is not None:
        clauses.append("source_ref = ?")
        params.append(criteria.source_ref)
    if criteria.written_by_agent is True:
        clauses.append("source_ref IS NOT NULL")
    elif criteria.written_by_agent is False:
        clauses.append("source_ref IS NULL")

    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    tail = f" LIMIT {int(criteria.limit)}" if criteria.limit else ""
    rows = conn.execute(
        f"SELECT {_ITEM_COLUMNS} FROM tracker_items{where} ORDER BY created_at, external_ref{tail}",
        tuple(params),
    ).fetchall()
    return [row_to_item(row) for row in rows]


def update_status(conn: sqlite3.Connection, external_ref: str, status: str, when: str) -> None:
    conn.execute(
        "UPDATE tracker_items SET status = ?, updated_at = ? WHERE external_ref = ?",
        (status, when, external_ref),
    )


def clear_seeded(conn: sqlite3.Connection) -> int:
    """Remove the seeded backlog only. Agent-written items are left alone."""
    return conn.execute("DELETE FROM tracker_items WHERE seeded = 1").rowcount


def count_items(conn: sqlite3.Connection, *, written_by_agent: bool | None = None) -> int:
    clause = ""
    if written_by_agent is True:
        clause = " WHERE source_ref IS NOT NULL"
    elif written_by_agent is False:
        clause = " WHERE source_ref IS NULL"
    return int(conn.execute(f"SELECT COUNT(*) AS n FROM tracker_items{clause}").fetchone()["n"])


# --- tracker_writes ----------------------------------------------------------


def record_write(
    conn: sqlite3.Connection, extraction_id: str, external_ref: str, provider: str, payload: dict
) -> None:
    """Insert the audit row.

    The database refuses this when the extraction is not approved, and refuses
    a second row for the same extraction. Both are why this is one statement
    rather than a check followed by an insert.
    """
    conn.execute(
        "INSERT INTO tracker_writes (id, extraction_id, external_ref, provider, write_payload, written_at)"
        " VALUES (?,?,?,?,?,?)",
        (
            str(uuid.uuid4()),
            extraction_id,
            external_ref,
            provider,
            json.dumps(payload),
            datetime.now(timezone.utc).isoformat(),
        ),
    )


def get_write(conn: sqlite3.Connection, extraction_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT extraction_id, external_ref, provider, write_payload, written_at"
        " FROM tracker_writes WHERE extraction_id = ?",
        (extraction_id,),
    ).fetchone()


def count_writes(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) AS n FROM tracker_writes").fetchone()["n"])


# --- tracker_write_attempts --------------------------------------------------


def record_attempt(
    conn: sqlite3.Connection,
    extraction_id: str,
    outcome: WriteOutcome,
    provider: str,
    *,
    external_ref: str | None = None,
    reason: str | None = None,
) -> WriteAttempt:
    attempt = WriteAttempt(
        id=str(uuid.uuid4()),
        extraction_id=extraction_id,
        outcome=outcome,
        provider=provider,
        external_ref=external_ref,
        reason=reason,
        attempted_at=datetime.now(timezone.utc).isoformat(),
    )
    conn.execute(
        "INSERT INTO tracker_write_attempts (id, extraction_id, outcome, reason, external_ref,"
        " provider, attempted_at) VALUES (?,?,?,?,?,?,?)",
        (
            attempt.id,
            attempt.extraction_id,
            attempt.outcome.value,
            attempt.reason,
            attempt.external_ref,
            attempt.provider,
            attempt.attempted_at,
        ),
    )
    return attempt


def list_attempts(conn: sqlite3.Connection, extraction_id: str | None = None) -> list[WriteAttempt]:
    where, params = ("", ())
    if extraction_id:
        where, params = (" WHERE extraction_id = ?", (extraction_id,))
    rows = conn.execute(
        "SELECT id, extraction_id, outcome, reason, external_ref, provider, attempted_at"
        f" FROM tracker_write_attempts{where} ORDER BY attempted_at",
        params,
    ).fetchall()
    return [
        WriteAttempt(
            id=r["id"],
            extraction_id=r["extraction_id"],
            outcome=WriteOutcome(r["outcome"]),
            reason=r["reason"],
            external_ref=r["external_ref"],
            provider=r["provider"],
            attempted_at=r["attempted_at"],
        )
        for r in rows
    ]


def attempt_counts(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT outcome, COUNT(*) AS n FROM tracker_write_attempts GROUP BY outcome"
    ).fetchall()
    return {row["outcome"]: row["n"] for row in rows}
