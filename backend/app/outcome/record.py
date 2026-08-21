"""M11 - building and writing the versioned outcome record.

Approved items only. That is not a filter applied politely at the end: an
outcome record is the artefact a downstream agent acts on, so including a
pending item would route around the approval gate the whole system exists to
enforce, at the last possible moment and in the least visible place.

Written through the store adapter rather than to a path, so a real document
platform is one class away and this module never learns what a filesystem is.

Records are versioned per source and never overwritten. Approving three more
actions produces version 2 beside version 1, because a consumer that read
version 1 and acted on it should be able to see what it read.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone

from app.adapters.factory import get_store
from app.config import Settings, get_settings
from app.db import database
from app.db.repositories import extractions as extraction_repo
from app.db.repositories import reviews as review_repo
from app.db.repositories import sources as source_repo
from app.errors import ConsentRefused, NotFoundError
from app.models.common import ExtractionType, ReviewStatus
from app.models.outcome import SCHEMA_VERSION, OutcomeCitation, OutcomeItem, OutcomeRecord

logger = logging.getLogger("agent.outcome")


def record_key(source_id: str, version: int) -> str:
    """Where a record lives in the store.

    The version is in the filename as well as inside the document, so the store
    can be listed and understood without opening anything.
    """
    return f"outcome_records/{source_id}/v{version:03d}.json"


def _to_item(extraction, source, approvals: dict[str, tuple[str | None, str | None]]) -> OutcomeItem:
    approved_by, approved_at = approvals.get(extraction.id, (extraction.reviewer, extraction.reviewed_at))
    return OutcomeItem(
        id=extraction.id,
        type=extraction.extraction_type.value,
        payload=extraction.payload,
        citation=OutcomeCitation(
            source_id=extraction.source_id,
            source_title=source.title,
            speaker=extraction.speaker,
            timestamp=extraction.timestamp,
            quote=extraction.verbatim_quote,
            quote_verified=extraction.quote_verified,
            char_start=extraction.quote_location.char_start if extraction.quote_location else None,
            char_end=extraction.quote_location.char_end if extraction.quote_location else None,
        ),
        confidence=extraction.confidence,
        approved_by=approved_by,
        approved_at=approved_at,
        edited_by_reviewer=extraction.payload != extraction.original_payload,
    )


def build_record(source_id: str, settings: Settings | None = None, *, version: int = 1) -> OutcomeRecord:
    """Assemble the record for one source. Reads, writes nothing."""
    cfg = settings or get_settings()

    with database.connect(cfg) as conn:
        source = source_repo.get_source(conn, source_id)
        if source is None:
            raise NotFoundError(f"no source with id {source_id}")

        approved = extraction_repo.list_extractions(conn, source_id=source_id, status=ReviewStatus.APPROVED)
        counts = extraction_repo.counts_by_status(conn, source_id)
        approvals = {
            e.id: next(
                (
                    (event.actor, event.created_at)
                    for event in review_repo.history(conn, e.id)
                    if event.event_type is review_repo.ReviewEventType.APPROVED
                ),
                (e.reviewer, e.reviewed_at),
            )
            for e in approved
        }

    if not source.consent_flag:
        # A record for a non-consented source would be an artefact derived from
        # content that was never processed. There is nothing to put in it, and
        # producing an empty one would imply the source was handled.
        raise ConsentRefused(
            f"source {source_id} withheld consent, so it has no approved items and no outcome record"
        )

    by_type = {kind: [e for e in approved if e.extraction_type is kind] for kind in ExtractionType}

    return OutcomeRecord(
        schema_version=SCHEMA_VERSION,
        record_version=version,
        record_id=str(uuid.uuid4()),
        source_id=source.id,
        source_title=source.title,
        source_type=source.source_type.value,
        meeting_date=source.meeting_date,
        participants=source.participants,
        consent_flag=source.consent_flag,
        generated_at=datetime.now(timezone.utc).isoformat(),
        actions=[_to_item(e, source, approvals) for e in by_type[ExtractionType.ACTION]],
        decisions=[_to_item(e, source, approvals) for e in by_type[ExtractionType.DECISION]],
        risks=[_to_item(e, source, approvals) for e in by_type[ExtractionType.RISK]],
        signals=[_to_item(e, source, approvals) for e in by_type[ExtractionType.SIGNAL]],
        pending_not_included=counts.get("pending", 0),
        rejected_not_included=counts.get("rejected", 0),
        expired_not_included=counts.get("expired", 0),
    )


def next_version(conn, source_id: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(record_version), 0) AS v FROM outcome_records WHERE source_id = ?",
        (source_id,),
    ).fetchone()
    return int(row["v"]) + 1


def emit(source_id: str, settings: Settings | None = None) -> OutcomeRecord:
    """Build the record, write it through the store, and index it.

    A new version every time rather than an overwrite. A consumer that read
    version 1 and acted on it should still be able to see what it read.
    """
    cfg = settings or get_settings()
    store = get_store(cfg)

    with database.connect(cfg) as conn:
        version = next_version(conn, source_id)

    record = build_record(source_id, cfg, version=version)
    content = record.model_dump_json(indent=2)
    key = record_key(source_id, version)

    document = store.write(key, content)

    with database.transaction(cfg) as conn:
        conn.execute(
            "INSERT INTO outcome_records (id, source_id, schema_version, record_version,"
            " consent_flag, content, file_path, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                record.record_id,
                source_id,
                record.schema_version,
                version,
                int(record.consent_flag),
                content,
                document.location or key,
                record.generated_at,
            ),
        )

    logger.info("wrote outcome record %s v%s with %s item(s)", source_id, version, record.total_items)
    return record


def load_record(source_id: str, version: int | None = None, settings: Settings | None = None) -> OutcomeRecord | None:
    """Read a record back through the store, exactly as a consumer would.

    Deliberately goes through the store rather than the database. The whole
    claim of M11 is that a second process can consume one of these without the
    transcript store, and reading it back any other way would not test that.
    """
    cfg = settings or get_settings()

    if version is None:
        with database.connect(cfg) as conn:
            row = conn.execute(
                "SELECT MAX(record_version) AS v FROM outcome_records WHERE source_id = ?",
                (source_id,),
            ).fetchone()
        if not row or row["v"] is None:
            return None
        version = int(row["v"])

    content = get_store(cfg).read(record_key(source_id, version))
    return OutcomeRecord.model_validate_json(content) if content else None


def list_records(settings: Settings | None = None, source_id: str | None = None) -> list[dict]:
    cfg = settings or get_settings()
    where, params = ("", ())
    if source_id:
        where, params = (" WHERE source_id = ?", (source_id,))

    with database.connect(cfg) as conn:
        rows = conn.execute(
            "SELECT source_id, schema_version, record_version, consent_flag, file_path, created_at,"
            " json_array_length(json_extract(content, '$.actions')) AS actions,"
            " json_array_length(json_extract(content, '$.decisions')) AS decisions,"
            " json_array_length(json_extract(content, '$.risks')) AS risks,"
            " json_array_length(json_extract(content, '$.signals')) AS signals"
            f" FROM outcome_records{where} ORDER BY source_id, record_version DESC",
            params,
        ).fetchall()

    return [dict(row) for row in rows]
