"""Reads and writes over `sources` and `ingestion_reports` (M1, M2)."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone

from app.models.common import SourceStatus, SourceType
from app.models.source import IngestionReport, Source

_COLUMNS = (
    "id, title, source_type, meeting_date, participants, consent_flag, origin_format, "
    "file_path, content_hash, ingested_at, status, refusal_reason, error_detail"
)


def row_to_source(row: sqlite3.Row) -> Source:
    return Source(
        id=row["id"],
        title=row["title"],
        source_type=SourceType(row["source_type"]),
        meeting_date=row["meeting_date"],
        participants=json.loads(row["participants"] or "[]"),
        consent_flag=bool(row["consent_flag"]),
        origin_format=row["origin_format"],
        file_path=row["file_path"],
        content_hash=row["content_hash"],
        ingested_at=row["ingested_at"],
        status=SourceStatus(row["status"]),
        refusal_reason=row["refusal_reason"],
        error_detail=row["error_detail"],
    )


def upsert_source(conn: sqlite3.Connection, source: Source) -> Source:
    """Insert or replace a source row.

    Replacing cascades to segments, so re-ingesting a file rebuilds it cleanly
    rather than leaving orphaned segments from the previous parse.
    """
    conn.execute(
        f"INSERT OR REPLACE INTO sources ({_COLUMNS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            source.id,
            source.title,
            source.source_type.value,
            source.meeting_date,
            json.dumps(source.participants),
            int(source.consent_flag),
            source.origin_format,
            source.file_path,
            source.content_hash,
            source.ingested_at,
            source.status.value,
            source.refusal_reason,
            source.error_detail,
        ),
    )
    return source


def get_source(conn: sqlite3.Connection, source_id: str) -> Source | None:
    row = conn.execute(f"SELECT {_COLUMNS} FROM sources WHERE id = ?", (source_id,)).fetchone()
    return row_to_source(row) if row else None


def list_sources(
    conn: sqlite3.Connection,
    *,
    status: SourceStatus | None = None,
    source_type: SourceType | None = None,
) -> list[Source]:
    clauses, params = [], []
    if status is not None:
        clauses.append("status = ?")
        params.append(status.value)
    if source_type is not None:
        clauses.append("source_type = ?")
        params.append(source_type.value)

    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM sources{where} ORDER BY meeting_date DESC, ingested_at DESC",
        tuple(params),
    ).fetchall()
    return [row_to_source(row) for row in rows]


def delete_source(conn: sqlite3.Connection, source_id: str) -> None:
    conn.execute("DELETE FROM sources WHERE id = ?", (source_id,))


def save_ingestion_report(conn: sqlite3.Connection, report: IngestionReport) -> None:
    """One report per source. Replaced on re-ingestion."""
    conn.execute("DELETE FROM ingestion_reports WHERE source_id = ?", (report.source_id,))
    conn.execute(
        "INSERT INTO ingestion_reports (id, source_id, report, created_at) VALUES (?,?,?,?)",
        (
            str(uuid.uuid4()),
            report.source_id,
            report.model_dump_json(),
            datetime.now(timezone.utc).isoformat(),
        ),
    )


def get_ingestion_report(conn: sqlite3.Connection, source_id: str) -> IngestionReport | None:
    row = conn.execute(
        "SELECT report FROM ingestion_reports WHERE source_id = ? ORDER BY created_at DESC LIMIT 1",
        (source_id,),
    ).fetchone()
    return IngestionReport.model_validate_json(row["report"]) if row else None
