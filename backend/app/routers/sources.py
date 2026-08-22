"""HTTP surface for M1 ingestion and M2 consent.

Thin by design: parse the request, call the service, return the result. No
consent logic, no parsing and no SQL lives here.
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile

from app.config import get_settings
from app.db import database
from app.db.repositories import segments as segment_repo
from app.db.repositories import sources as source_repo
from app.extraction.chunker import chunk_segments
from app.ingestion.service import (
    IngestionOutcome,
    ingest_chat_export,
    ingest_from_manifest,
    ingest_transcript,
)
from app.models.chunk import Chunk
from app.models.common import SourceStatus, SourceType
from app.models.source import IngestionReport, Segment, Source, SourceMetadata

router = APIRouter(prefix="/api/sources", tags=["sources"])


@router.get("", response_model=list[Source], summary="List every ingested source")
def list_sources(
    status: SourceStatus | None = Query(default=None),
    source_type: SourceType | None = Query(default=None),
) -> list[Source]:
    """Refused and errored sources are listed alongside successful ones.

    A refusal is a record, not an omission: the demo has to show the
    non-consented meeting was seen and declined.
    """
    with database.connect() as conn:
        return source_repo.list_sources(conn, status=status, source_type=source_type)


@router.get("/{source_id}", response_model=Source, summary="One source")
def get_source(source_id: str) -> Source:
    with database.connect() as conn:
        source = source_repo.get_source(conn, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail=f"no source with id {source_id}")
    return source


@router.get("/{source_id}/segments", response_model=list[Segment], summary="Normalised segments")
def get_segments(source_id: str) -> list[Segment]:
    with database.connect() as conn:
        if source_repo.get_source(conn, source_id) is None:
            raise HTTPException(status_code=404, detail=f"no source with id {source_id}")
        return segment_repo.list_segments(conn, source_id)


@router.get("/{source_id}/report", response_model=IngestionReport, summary="What ingestion did")
def get_report(source_id: str) -> IngestionReport:
    """Includes the consent decision, bytes read and every defect found."""
    with database.connect() as conn:
        report = source_repo.get_ingestion_report(conn, source_id)
    if report is None:
        raise HTTPException(status_code=404, detail=f"no ingestion report for {source_id}")
    return report


@router.get("/{source_id}/text", summary="The exact text quotes are verified against")
def get_source_text(source_id: str) -> dict[str, object]:
    """Exposed because quote verification and character offsets both index into
    this string. A reviewer can check a citation by hand from this endpoint."""
    with database.connect() as conn:
        if source_repo.get_source(conn, source_id) is None:
            raise HTTPException(status_code=404, detail=f"no source with id {source_id}")
        text = segment_repo.get_source_text(conn, source_id)
    return {"source_id": source_id, "length": len(text), "text": text}


@router.get("/{source_id}/chunks", response_model=list[Chunk], summary="Exactly what the model is sent")
def get_chunks(source_id: str) -> list[Chunk]:
    """The chunks as the extractor builds them, verbatim.

    Exposed so the pipeline can be inspected rather than trusted. `text` is the
    transcript lines the model may quote from, and `context` is the non-quotable
    header naming the meeting, the participants and the time range. Reading this
    endpoint tells you precisely what went to the provider, which is the only
    way to check that the chunking strategy does what it is documented to do.

    Computed on demand from the stored segments and the current settings, so it
    always reflects the configuration a run would actually use rather than a
    snapshot taken when the source was ingested.
    """
    settings = get_settings()
    with database.connect() as conn:
        source = source_repo.get_source(conn, source_id)
        if source is None:
            raise HTTPException(status_code=404, detail=f"no source with id {source_id}")
        segments = segment_repo.list_segments(conn, source_id)

    return chunk_segments(source, segments, settings)


@router.post("/ingest", response_model=IngestionOutcome, summary="Ingest a source already on disk")
def ingest(metadata: SourceMetadata, path: str | None = Query(default=None)) -> IngestionOutcome:
    """`path` is relative to the sample data directory when omitted from metadata."""
    settings = get_settings()
    resolved = Path(path) if path else (settings.sample_data_dir / (metadata.file_path or ""))
    return ingest_transcript(metadata, resolved, settings=settings)


@router.post("/upload", response_model=IngestionOutcome, summary="Upload and ingest a transcript")
async def upload(
    file: UploadFile = File(...),
    source_id: str = Form(...),
    title: str = Form(...),
    consent_flag: bool = Form(...),
    source_type: str = Form(default="transcript"),
    meeting_date: str | None = Form(default=None),
    participants: str = Form(default="[]"),
) -> IngestionOutcome:
    """One endpoint for both kinds of source, because one gate serves both.

    A transcript and a chat export differ only in which parser reads the bytes.
    Everything around that is identical: the same consent check on the metadata
    before the file is opened, the same refusal leaving nothing on disk, the
    same report shape coming back. A second endpoint would mean a second copy
    of the gate, and the gate is the one thing in this application that must
    exist exactly once.

    The consent flag is a required form field with no default. An upload that
    does not state consent is rejected by validation, which is the same rule
    the manifest path follows.
    """
    kind = (source_type or "").strip().lower()
    if kind == "audio":
        # Named rather than silently misrouted to the transcript parser, which
        # would report a parse failure for a file nothing here can read.
        raise HTTPException(
            status_code=422,
            detail="audio ingestion is not built. Upload a transcript or a chat export.",
        )
    if kind not in {"transcript", "chat_export"}:
        raise HTTPException(
            status_code=422,
            detail=f"unknown source_type '{source_type}'. Use 'transcript' or 'chat_export'.",
        )

    try:
        participant_list = json.loads(participants)
    except json.JSONDecodeError:
        participant_list = [p.strip() for p in participants.split(",") if p.strip()]

    metadata = SourceMetadata(
        id=source_id,
        title=title,
        source_type=SourceType(kind),
        consent_flag=consent_flag,
        meeting_date=meeting_date,
        participants=participant_list,
        file_path=file.filename,
    )

    settings = get_settings()
    upload_dir = settings.db_path.parent / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    target = upload_dir / f"{source_id}_{file.filename}"

    # Written to disk before the consent gate runs, and deleted again if the
    # gate refuses, so a non-consented upload leaves nothing behind.
    target.write_bytes(await file.read())

    ingest = ingest_chat_export if kind == "chat_export" else ingest_transcript
    outcome = ingest(metadata, target, settings=settings)
    if outcome.source.status is SourceStatus.REFUSED:
        target.unlink(missing_ok=True)
    return outcome


@router.post("/seed", response_model=list[IngestionOutcome], summary="Ingest the committed sample data")
def seed() -> list[IngestionOutcome]:
    return ingest_from_manifest(get_settings())
