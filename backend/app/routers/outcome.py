"""HTTP surface for M11."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from app.config import get_settings
from app.models.outcome import OutcomeRecord
from app.outcome import record as outcome_service

router = APIRouter(prefix="/api/outcomes", tags=["outcome records"])


@router.get("", summary="Every record written")
def list_records(source_id: str | None = Query(default=None)) -> list[dict]:
    return outcome_service.list_records(get_settings(), source_id)


@router.get("/{source_id}", response_model=OutcomeRecord, summary="Read a record back")
def get_record(source_id: str, version: int | None = Query(default=None)) -> OutcomeRecord:
    """Read through the document store, exactly as a consumer would.

    Not from the database. The claim M11 makes is that a second process can
    consume one of these without the transcript store, and reading it back any
    other way would not test that.
    """
    record = outcome_service.load_record(source_id, version, get_settings())
    if record is None:
        raise HTTPException(status_code=404, detail=f"no outcome record for {source_id}")
    return record


@router.post("/{source_id}", response_model=OutcomeRecord, summary="Emit a new version")
def emit(source_id: str) -> OutcomeRecord:
    """Writes a new version rather than overwriting.

    Refuses with 403 for a source that withheld consent: a record for one would
    be an artefact derived from content that was never processed.
    """
    return outcome_service.emit(source_id, get_settings())
