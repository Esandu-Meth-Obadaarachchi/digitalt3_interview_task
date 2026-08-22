"""Reads and writes over `chat_messages` (M9).

Two schema constraints do work here that no code needs to repeat:

  CHECK (is_direct_message = 0)   a DM cannot be stored, at all
  CHECK (classification IN (...))  'noise' is not a storable value, because
                                   noise is discarded rather than recorded
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from app.ingestion.chat_export import ChatMessage
from app.models.common import SignalClass, StrictModel

_COLUMNS = (
    "id, external_id, source_id, channel, author, ts, thread_id, text, is_direct_message, "
    "classification, classification_confidence, classified_at"
)


def stored_id(source_id: str, external_id: str) -> str:
    """The primary key for one message.

    Namespaced by source, exactly as segments are. A message id is unique
    inside its own export and nowhere else, so two exports numbering from
    msg_001 collide the moment the second one is uploaded.
    """
    return f"{source_id}::{external_id}"


class StoredMessage(StrictModel):
    #: Namespaced by source: the primary key.
    id: str
    #: The id the export itself used, kept so a message can be traced back to
    #: the system it came from, and short enough to show a person.
    external_id: str
    source_id: str
    channel: str
    author: str
    ts: str
    text: str
    thread_id: str | None = None
    classification: SignalClass | None = None
    classification_confidence: float | None = None
    classified_at: str | None = None


def _row(row: sqlite3.Row) -> StoredMessage:
    return StoredMessage(
        id=row["id"],
        external_id=row["external_id"],
        source_id=row["source_id"],
        channel=row["channel"],
        author=row["author"],
        ts=row["ts"],
        text=row["text"],
        thread_id=row["thread_id"],
        classification=SignalClass(row["classification"]) if row["classification"] else None,
        classification_confidence=row["classification_confidence"],
        classified_at=row["classified_at"],
    )


def replace_messages(conn: sqlite3.Connection, source_id: str, messages: list[ChatMessage]) -> int:
    """Store the parsed messages as classification candidates.

    Every one of these came from a project channel: the parser never returns a
    direct message, and the schema would refuse it if it did.
    """
    conn.execute("DELETE FROM chat_messages WHERE source_id = ?", (source_id,))
    conn.executemany(
        f"INSERT INTO chat_messages ({_COLUMNS}) VALUES (?,?,?,?,?,?,?,?,0,NULL,NULL,NULL)",
        [
            (stored_id(source_id, m.id), m.id, source_id, m.channel, m.author, m.ts,
             m.thread_id, m.text)
            for m in messages
        ],
    )
    return len(messages)


def list_messages(
    conn: sqlite3.Connection,
    *,
    source_id: str | None = None,
    channel: str | None = None,
    classification: SignalClass | None = None,
    unclassified: bool | None = None,
    limit: int | None = None,
) -> list[StoredMessage]:
    clauses, params = [], []
    if source_id:
        clauses.append("source_id = ?")
        params.append(source_id)
    if channel:
        clauses.append("channel = ?")
        params.append(channel)
    if classification:
        clauses.append("classification = ?")
        params.append(classification.value)
    if unclassified is True:
        clauses.append("classification IS NULL")
    elif unclassified is False:
        clauses.append("classification IS NOT NULL")

    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    tail = f" LIMIT {int(limit)}" if limit else ""
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM chat_messages{where} ORDER BY channel, ts, id{tail}", tuple(params)
    ).fetchall()
    return [_row(r) for r in rows]


def get_message(conn: sqlite3.Connection, message_id: str) -> StoredMessage | None:
    row = conn.execute(f"SELECT {_COLUMNS} FROM chat_messages WHERE id = ?", (message_id,)).fetchone()
    return _row(row) if row else None


def classify(conn: sqlite3.Connection, message_id: str, label: SignalClass, confidence: float) -> None:
    conn.execute(
        "UPDATE chat_messages SET classification = ?, classification_confidence = ?,"
        " classified_at = ? WHERE id = ?",
        (label.value, confidence, datetime.now(timezone.utc).isoformat(), message_id),
    )


def discard(conn: sqlite3.Connection, message_ids: list[str]) -> int:
    """Delete messages classified as noise.

    The brief is explicit: noise is discarded, not stored. The schema enforces
    the same rule from the other direction, since 'noise' is absent from the
    classification CHECK and could not be written even deliberately.
    """
    if not message_ids:
        return 0
    marks = ",".join("?" * len(message_ids))
    return conn.execute(
        f"DELETE FROM chat_messages WHERE id IN ({marks})", tuple(message_ids)
    ).rowcount


def counts_by_class(conn: sqlite3.Connection, source_id: str | None = None) -> dict[str, int]:
    where, params = ("", ())
    if source_id:
        where, params = (" WHERE source_id = ?", (source_id,))
    rows = conn.execute(
        f"SELECT COALESCE(classification, 'unclassified') AS c, COUNT(*) AS n"
        f" FROM chat_messages{where} GROUP BY c",
        params,
    ).fetchall()
    return {r["c"]: r["n"] for r in rows}


def channels(conn: sqlite3.Connection, source_id: str | None = None) -> list[str]:
    where, params = ("", ())
    if source_id:
        where, params = (" WHERE source_id = ?", (source_id,))
    return [
        r["channel"]
        for r in conn.execute(f"SELECT DISTINCT channel FROM chat_messages{where} ORDER BY channel", params)
    ]
