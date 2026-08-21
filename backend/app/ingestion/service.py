"""M1 + M2 - the ingestion pipeline.

    metadata -> consent gate -> read -> parse -> validate -> normalise -> store

The consent gate is the first step and runs on metadata alone. A refused
source never reaches `read`, so its report carries `bytes_read: 0` as evidence
the file was never opened.

Three outcomes, all of them recorded:

    ingested  segments stored, warnings travel with the source
    refused   consent withheld, nothing read, refusal reason stored
    error     blocking defect found, nothing stored beyond the reason

The store is never left half-written: segments are only inserted once the
whole file has parsed and validated, inside one transaction.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from app.config import Settings, get_settings
from app.db import database
from app.db.repositories import segments as segment_repo
from app.db.repositories import sources as source_repo
from app.ingestion.consent import evaluate_consent
from app.ingestion.normaliser import normalise
from app.ingestion.parsers import detect_format, parse_transcript
from app.ingestion.validator import summarise, validate
from app.models.common import SourceStatus, SourceType, StrictModel
from app.models.ingestion import Defect, DefectCode, DefectSeverity, TranscriptFormat
from app.models.source import IngestionReport, Segment, Source, SourceMetadata


class IngestionOutcome(StrictModel):
    """The result of one ingestion attempt, whatever happened."""

    source: Source
    report: IngestionReport
    segments: list[Segment] = []

    @property
    def ok(self) -> bool:
        return self.report.ok


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _refusal(metadata: SourceMetadata, reason: str, decision) -> IngestionOutcome:
    """Build the record for a source that was never opened."""
    source = Source(
        id=metadata.id,
        title=metadata.title,
        source_type=metadata.source_type,
        meeting_date=metadata.meeting_date,
        participants=metadata.participants,
        consent_flag=metadata.consent_flag,
        origin_format=None,
        file_path=metadata.file_path,
        content_hash=None,
        ingested_at=_now(),
        status=SourceStatus.REFUSED,
        refusal_reason=reason,
    )
    report = IngestionReport(
        source_id=metadata.id,
        ok=False,
        status=SourceStatus.REFUSED,
        consent=decision,
        bytes_read=0,
        rejection_reason=reason,
    )
    return IngestionOutcome(source=source, report=report, segments=[])


def ingest_transcript(
    metadata: SourceMetadata,
    path: Path | None = None,
    settings: Settings | None = None,
    *,
    persist: bool = True,
    check_truncation: bool = True,
) -> IngestionOutcome:
    """Run one source through the pipeline and store the outcome."""
    cfg = settings or get_settings()

    # --- 1. Consent gate, before anything is opened --------------------------
    decision = evaluate_consent(metadata)
    if decision.refused:
        outcome = _refusal(metadata, decision.reason, decision)
        if persist:
            _persist(outcome, cfg)
        return outcome

    resolved = path or (cfg.sample_data_dir / (metadata.file_path or ""))

    # --- 2. Read ------------------------------------------------------------
    read = read_source(resolved)
    defects: list[Defect] = list(read.defects)

    # --- 2a. Already stored, byte for byte? ---------------------------------
    # Re-ingesting an unchanged file rewrites the segment rows, and deleting a
    # segment sets extractions.segment_id to NULL, orphaning citations that
    # were perfectly good. When the content hash matches what is stored there
    # is nothing to do, so nothing is done.
    if read.content_hash:
        with database.connect(cfg) as conn:
            stored = source_repo.get_source(conn, metadata.id)
            previous = source_repo.get_ingestion_report(conn, metadata.id)

        if (
            stored is not None
            and stored.status is SourceStatus.INGESTED
            and stored.content_hash == read.content_hash
            and previous is not None
        ):
            with database.connect(cfg) as conn:
                existing_segments = segment_repo.list_segments(conn, metadata.id)
            return IngestionOutcome(
                source=stored,
                report=previous.model_copy(update={"unchanged": True, "consent": decision}),
                segments=existing_segments,
            )

    # --- 3. Parse -----------------------------------------------------------
    detected = detect_format(resolved, read.text)
    if read.text:
        parsed = parse_transcript(
            resolved, read.text, read.encoding, read.bytes_read, metadata.participants, fmt=detected
        )
        defects.extend(parsed.defects)
        raw_segments = parsed.raw_segments
        origin_format = parsed.origin_format
    else:
        raw_segments = []
        origin_format = detected or TranscriptFormat.TXT

    # --- 4. Validate --------------------------------------------------------
    from app.models.ingestion import ParseResult  # local import keeps the contract one-way

    combined = ParseResult(
        origin_format=origin_format,
        encoding=read.encoding,
        bytes_read=read.bytes_read,
        raw_segments=raw_segments,
        defects=defects,
    )
    combined = validate(combined, check_truncation=check_truncation)

    # --- 5. Reject, or normalise and store ----------------------------------
    if not combined.ok:
        reason = summarise(combined) or "source could not be parsed"
        source = Source(
            id=metadata.id,
            title=metadata.title,
            source_type=metadata.source_type,
            meeting_date=metadata.meeting_date,
            participants=metadata.participants,
            consent_flag=metadata.consent_flag,
            origin_format=combined.origin_format.value,
            file_path=metadata.file_path,
            content_hash=read.content_hash,
            ingested_at=_now(),
            status=SourceStatus.ERROR,
            error_detail=reason,
        )
        report = IngestionReport(
            source_id=metadata.id,
            ok=False,
            status=SourceStatus.ERROR,
            consent=decision,
            origin_format=combined.origin_format.value,
            encoding=read.encoding,
            bytes_read=read.bytes_read,
            content_hash=read.content_hash,
            segments_parsed=0,
            defects=combined.defects,
            rejection_reason=reason,
        )
        outcome = IngestionOutcome(source=source, report=report, segments=[])
        if persist:
            _persist(outcome, cfg)
        return outcome

    segments, _ = normalise(metadata.id, combined.raw_segments)
    spoke = sorted({s.speaker for s in segments if s.speaker})
    silent = [p for p in metadata.participants if p not in spoke]
    last = next((s for s in reversed(segments) if s.start_seconds is not None), None)

    source = Source(
        id=metadata.id,
        title=metadata.title,
        source_type=metadata.source_type,
        meeting_date=metadata.meeting_date,
        participants=metadata.participants,
        consent_flag=metadata.consent_flag,
        origin_format=combined.origin_format.value,
        file_path=metadata.file_path,
        content_hash=read.content_hash,
        ingested_at=_now(),
        status=SourceStatus.INGESTED,
    )
    report = IngestionReport(
        source_id=metadata.id,
        ok=True,
        status=SourceStatus.INGESTED,
        consent=decision,
        origin_format=combined.origin_format.value,
        encoding=read.encoding,
        bytes_read=read.bytes_read,
        content_hash=read.content_hash,
        segments_parsed=len(segments),
        speakers=spoke,
        silent_participants=silent,
        duration_seconds=last.start_seconds if last else None,
        defects=combined.defects,
    )
    outcome = IngestionOutcome(source=source, report=report, segments=segments)
    if persist:
        _persist(outcome, cfg)
    return outcome


def read_source(path: Path):
    """Indirection so tests can read from memory without touching the disk."""
    from app.ingestion.reader import read_source_text

    return read_source_text(path)


def _persist(outcome: IngestionOutcome, settings: Settings) -> None:
    """One transaction: the source, its segments and its report land together.

    `upsert_source` updates in place rather than replacing, so no cascade fires
    and existing extractions survive. Segments are still replaced, because the
    content genuinely changed if we reached here, and a citation into text that
    no longer exists should not silently keep pointing somewhere.
    """
    with database.transaction(settings) as conn:
        source_repo.upsert_source(conn, outcome.source)
        segment_repo.replace_segments(conn, outcome.source.id, outcome.segments)
        source_repo.save_ingestion_report(conn, outcome.report)


def ingest_chat_export(
    metadata: SourceMetadata,
    path: Path | None = None,
    settings: Settings | None = None,
    *,
    persist: bool = True,
) -> IngestionOutcome:
    """M9 ingestion. Same shape as a transcript, same gate, different parser.

    Direct messages never reach the store: the parser drops them and the schema
    would refuse them. The count of what was dropped is on the report, because
    the messages themselves leave no trace and "zero DM records" is otherwise
    indistinguishable from "the export had none".

    Classification is a separate step. Ingestion needs no model, so it works
    with no key and no quota, exactly like a transcript.
    """
    from app.db.repositories import chat as chat_repo
    from app.ingestion.chat_export import read_chat_export

    cfg = settings or get_settings()

    decision = evaluate_consent(metadata)
    if decision.refused:
        outcome = _refusal(metadata, decision.reason, decision)
        if persist:
            with database.transaction(cfg) as conn:
                source_repo.upsert_source(conn, outcome.source)
                source_repo.save_ingestion_report(conn, outcome.report)
        return outcome

    resolved = path or (cfg.sample_data_dir / (metadata.file_path or ""))
    parsed = read_chat_export(resolved)
    blocking = [d for d in parsed.defects if d.blocking]

    source = Source(
        id=metadata.id,
        title=metadata.title,
        source_type=metadata.source_type,
        meeting_date=metadata.meeting_date,
        participants=metadata.participants,
        consent_flag=metadata.consent_flag,
        origin_format="json",
        file_path=metadata.file_path,
        content_hash=None,
        ingested_at=_now(),
        status=SourceStatus.ERROR if blocking else SourceStatus.INGESTED,
        error_detail=(blocking[0].detail if blocking else None),
    )
    report = IngestionReport(
        source_id=metadata.id,
        ok=not blocking,
        status=source.status,
        consent=decision,
        origin_format="json",
        encoding="utf-8",
        bytes_read=resolved.stat().st_size if resolved.exists() else 0,
        messages_parsed=0 if blocking else len(parsed.messages),
        direct_messages_excluded=parsed.direct_messages_excluded,
        speakers=sorted({m.author for m in parsed.messages}),
        defects=parsed.defects,
        rejection_reason=(blocking[0].detail if blocking else None),
    )

    if persist:
        with database.transaction(cfg) as conn:
            source_repo.upsert_source(conn, source)
            if not blocking:
                chat_repo.replace_messages(conn, metadata.id, parsed.messages)
            source_repo.save_ingestion_report(conn, report)

    return IngestionOutcome(source=source, report=report, segments=[])


def ingest_from_manifest(settings: Settings | None = None) -> list[IngestionOutcome]:
    """Ingest every source declared in the sample-data manifest."""
    import json

    cfg = settings or get_settings()
    manifest = json.loads((cfg.sample_data_dir / "metadata" / "sources.json").read_text(encoding="utf-8"))

    outcomes: list[IngestionOutcome] = []
    for entry in manifest["sources"]:
        metadata = SourceMetadata(**entry)
        if metadata.source_type is SourceType.TRANSCRIPT:
            outcomes.append(ingest_transcript(metadata, settings=cfg))
        elif metadata.source_type is SourceType.CHAT_EXPORT:
            outcomes.append(ingest_chat_export(metadata, settings=cfg))
    return outcomes


__all__ = ["IngestionOutcome", "ingest_transcript", "ingest_chat_export", "ingest_from_manifest"]
