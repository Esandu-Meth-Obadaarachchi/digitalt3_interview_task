"""Reads and writes over `segments` (M1).

Inserting a segment fires the FTS5 sync trigger, so the keyword index is never
rebuilt separately and cannot drift from the table it indexes.
"""

from __future__ import annotations

import sqlite3

from app.ingestion.normaliser import JOIN
from app.models.source import Segment

_COLUMNS = (
    "id, source_id, segment_index, speaker, start_ts, end_ts, start_seconds, "
    "text, char_start, char_end"
)


def row_to_segment(row: sqlite3.Row) -> Segment:
    return Segment(
        id=row["id"],
        source_id=row["source_id"],
        segment_index=row["segment_index"],
        speaker=row["speaker"],
        start_ts=row["start_ts"],
        end_ts=row["end_ts"],
        start_seconds=row["start_seconds"],
        text=row["text"],
        char_start=row["char_start"],
        char_end=row["char_end"],
    )


def replace_segments(conn: sqlite3.Connection, source_id: str, segments: list[Segment]) -> int:
    conn.execute("DELETE FROM segments WHERE source_id = ?", (source_id,))
    conn.executemany(
        f"INSERT INTO segments ({_COLUMNS}) VALUES (?,?,?,?,?,?,?,?,?,?)",
        [
            (
                s.id,
                s.source_id,
                s.segment_index,
                s.speaker,
                s.start_ts,
                s.end_ts,
                s.start_seconds,
                s.text,
                s.char_start,
                s.char_end,
            )
            for s in segments
        ],
    )
    return len(segments)


def list_segments(conn: sqlite3.Connection, source_id: str) -> list[Segment]:
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM segments WHERE source_id = ? ORDER BY segment_index",
        (source_id,),
    ).fetchall()
    return [row_to_segment(row) for row in rows]


def get_segment(conn: sqlite3.Connection, segment_id: str) -> Segment | None:
    row = conn.execute(f"SELECT {_COLUMNS} FROM segments WHERE id = ?", (segment_id,)).fetchone()
    return row_to_segment(row) if row else None


def get_source_text(conn: sqlite3.Connection, source_id: str) -> str:
    """The exact string quote verification and character offsets index into.

    Rebuilt with the same join the normaliser used, so an offset stored at
    ingestion still resolves after a restart.
    """
    rows = conn.execute(
        "SELECT text FROM segments WHERE source_id = ? ORDER BY segment_index", (source_id,)
    ).fetchall()
    return JOIN.join(row["text"] for row in rows)


def count_segments(conn: sqlite3.Connection, source_id: str) -> int:
    row = conn.execute("SELECT COUNT(*) AS n FROM segments WHERE source_id = ?", (source_id,)).fetchone()
    return int(row["n"])


def speakers(conn: sqlite3.Connection, source_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT speaker FROM segments WHERE source_id = ? AND speaker IS NOT NULL ORDER BY speaker",
        (source_id,),
    ).fetchall()
    return [row["speaker"] for row in rows]
