"""Reads and writes over `extractions` (M3, M4, M5, M6, M9).

Every write here goes through the database's triggers, so the consent gate and
the review-state rules apply whether a caller used the service layer or not.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

from app.models.common import ExtractionType, ReviewStatus
from app.models.extraction import Extraction, QuoteLocation

_COLUMNS = (
    "id, source_id, extraction_type, payload, original_payload, search_text, verbatim_quote, "
    "quote_verified, speaker, timestamp, segment_id, message_id, char_start, char_end, "
    "confidence, dedup_key, chunk_id, merged_from, merge_reason, provider, model_name, "
    "prompt_version, status, reviewer, reviewed_at, review_note, expires_at, created_at"
)


def row_to_extraction(row: sqlite3.Row) -> Extraction:
    location = None
    if row["char_start"] is not None and row["char_end"] is not None:
        location = QuoteLocation(
            char_start=row["char_start"], char_end=row["char_end"], segment_id=row["segment_id"]
        )

    return Extraction(
        id=row["id"],
        source_id=row["source_id"],
        extraction_type=ExtractionType(row["extraction_type"]),
        payload=json.loads(row["payload"]),
        original_payload=json.loads(row["original_payload"]),
        verbatim_quote=row["verbatim_quote"],
        quote_verified=bool(row["quote_verified"]),
        quote_location=location,
        speaker=row["speaker"],
        timestamp=row["timestamp"],
        segment_id=row["segment_id"],
        message_id=row["message_id"],
        confidence=row["confidence"],
        dedup_key=row["dedup_key"],
        chunk_id=row["chunk_id"],
        merged_from=json.loads(row["merged_from"] or "[]"),
        provider=row["provider"],
        model_name=row["model_name"],
        prompt_version=row["prompt_version"],
        status=ReviewStatus(row["status"]),
        reviewer=row["reviewer"],
        reviewed_at=row["reviewed_at"],
        review_note=row["review_note"],
        expires_at=row["expires_at"],
        created_at=row["created_at"],
    )


def search_text_for(payload: dict, quote: str) -> str:
    """What the FTS index and the embeddings see for this extraction.

    The payload's own words plus the quote, so a question can be answered from
    an approved extraction as well as from the raw transcript.
    """
    parts = [str(v) for v in payload.values() if isinstance(v, (str, int, float))]
    parts.append(quote)
    return " ".join(p for p in parts if p)


def insert(
    conn: sqlite3.Connection,
    extraction: Extraction,
    *,
    merge_reason: str | None = None,
    expiry_hours: int | None = None,
) -> Extraction:
    """Insert a pending extraction.

    Raises ConsentRefused via the trigger if the source withheld consent, which
    is the backstop under the service-layer check.
    """
    expires_at = extraction.expires_at
    if expires_at is None and expiry_hours:
        expires_at = (
            datetime.fromisoformat(extraction.created_at) + timedelta(hours=expiry_hours)
        ).isoformat()

    conn.execute(
        f"INSERT INTO extractions ({_COLUMNS}) VALUES ({','.join('?' * 28)})",
        (
            extraction.id,
            extraction.source_id,
            extraction.extraction_type.value,
            json.dumps(extraction.payload),
            json.dumps(extraction.original_payload),
            search_text_for(extraction.payload, extraction.verbatim_quote),
            extraction.verbatim_quote,
            int(extraction.quote_verified),
            extraction.speaker,
            extraction.timestamp,
            extraction.quote_location.segment_id if extraction.quote_location else extraction.segment_id,
            extraction.message_id,
            extraction.quote_location.char_start if extraction.quote_location else None,
            extraction.quote_location.char_end if extraction.quote_location else None,
            extraction.confidence,
            extraction.dedup_key,
            extraction.chunk_id,
            json.dumps(extraction.merged_from),
            merge_reason,
            extraction.provider,
            extraction.model_name,
            extraction.prompt_version,
            extraction.status.value,
            extraction.reviewer,
            extraction.reviewed_at,
            extraction.review_note,
            expires_at,
            extraction.created_at,
        ),
    )
    return extraction.model_copy(update={"expires_at": expires_at})


def get(conn: sqlite3.Connection, extraction_id: str) -> Extraction | None:
    row = conn.execute(f"SELECT {_COLUMNS} FROM extractions WHERE id = ?", (extraction_id,)).fetchone()
    return row_to_extraction(row) if row else None


def list_extractions(
    conn: sqlite3.Connection,
    *,
    source_id: str | None = None,
    extraction_type: ExtractionType | None = None,
    status: ReviewStatus | None = None,
    quote_verified: bool | None = None,
    limit: int | None = None,
) -> list[Extraction]:
    clauses, params = [], []
    if source_id:
        clauses.append("source_id = ?")
        params.append(source_id)
    if extraction_type:
        clauses.append("extraction_type = ?")
        params.append(extraction_type.value)
    if status:
        clauses.append("status = ?")
        params.append(status.value)
    if quote_verified is not None:
        clauses.append("quote_verified = ?")
        params.append(int(quote_verified))

    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    tail = f" LIMIT {int(limit)}" if limit else ""
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM extractions{where}"
        f" ORDER BY quote_verified ASC, confidence DESC, char_start ASC{tail}",
        tuple(params),
    ).fetchall()
    return [row_to_extraction(row) for row in rows]


def delete_for_source(
    conn: sqlite3.Connection, source_id: str, extraction_type: ExtractionType | None = None
) -> int:
    """Remove pending extractions before a re-run.

    Only pending ones. An approved or rejected item is a human decision and a
    re-extraction has no business discarding it.
    """
    clauses = ["source_id = ?", "status = 'pending'"]
    params: list[object] = [source_id]
    if extraction_type:
        clauses.append("extraction_type = ?")
        params.append(extraction_type.value)

    cursor = conn.execute(f"DELETE FROM extractions WHERE {' AND '.join(clauses)}", tuple(params))
    return cursor.rowcount


def update_payload(conn: sqlite3.Connection, extraction_id: str, payload: dict, quote: str) -> None:
    """Apply a human edit. `original_payload` is immutable by trigger."""
    conn.execute(
        "UPDATE extractions SET payload = ?, search_text = ? WHERE id = ?",
        (json.dumps(payload), search_text_for(payload, quote), extraction_id),
    )


def set_status(
    conn: sqlite3.Connection,
    extraction_id: str,
    status: ReviewStatus,
    reviewer: str | None,
    note: str | None = None,
) -> None:
    conn.execute(
        "UPDATE extractions SET status = ?, reviewer = ?, reviewed_at = ?, review_note = ? WHERE id = ?",
        (
            status.value,
            reviewer,
            datetime.now(timezone.utc).isoformat() if reviewer else None,
            note,
            extraction_id,
        ),
    )


def counts_by_status(conn: sqlite3.Connection, source_id: str | None = None) -> dict[str, int]:
    where, params = ("", ())
    if source_id:
        where, params = (" WHERE source_id = ?", (source_id,))
    rows = conn.execute(
        f"SELECT status, COUNT(*) AS n FROM extractions{where} GROUP BY status", params
    ).fetchall()
    return {row["status"]: row["n"] for row in rows}


def exists_with_dedup_key(
    conn: sqlite3.Connection, source_id: str, extraction_type: ExtractionType, dedup_key: str
) -> bool:
    row = conn.execute(
        "SELECT 1 FROM extractions WHERE source_id = ? AND extraction_type = ? AND dedup_key = ?",
        (source_id, extraction_type.value, dedup_key),
    ).fetchone()
    return row is not None
