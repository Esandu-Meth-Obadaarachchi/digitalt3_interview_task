"""HTTP surface for M3 extraction runs."""

from __future__ import annotations

from fastapi import APIRouter, Query

from app.config import get_settings
from app.db import database
from app.db.repositories import extractions as extraction_repo
from app.db.repositories import llm_calls as llm_call_repo
from app.extraction.actions import ExtractionRun, extract_actions
from app.models.common import ExtractionType, ReviewStatus
from app.models.extraction import Extraction
from app.models.telemetry import UsageSummary

router = APIRouter(prefix="/api/extractions", tags=["extractions"])


@router.post("/{source_id}/actions", response_model=ExtractionRun, summary="Run M3 over a source")
def run_action_extraction(source_id: str, replace_pending: bool = Query(default=True)) -> ExtractionRun:
    """Refuses with 403 when the source withheld consent.

    That check lives in the extraction service, not here, so it holds however
    the run was triggered.
    """
    return extract_actions(source_id, get_settings(), replace_pending=replace_pending)


@router.get("", response_model=list[Extraction], summary="List extractions")
def list_extractions(
    source_id: str | None = Query(default=None),
    extraction_type: ExtractionType | None = Query(default=None),
    status: ReviewStatus | None = Query(default=None),
    quote_verified: bool | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1, le=500),
) -> list[Extraction]:
    with database.connect() as conn:
        return extraction_repo.list_extractions(
            conn,
            source_id=source_id,
            extraction_type=extraction_type,
            status=status,
            quote_verified=quote_verified,
            limit=limit,
        )


@router.get("/usage", response_model=UsageSummary, summary="Model cost and retry statistics")
def usage(source_id: str | None = Query(default=None), capability: str | None = Query(default=None)) -> UsageSummary:
    with database.connect() as conn:
        return llm_call_repo.summarise(conn, source_id=source_id, capability=capability)
