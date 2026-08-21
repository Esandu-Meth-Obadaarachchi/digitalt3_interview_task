"""HTTP surface for M3 extraction runs."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Query

from app.config import get_settings
from app.errors import AgentError
from app.db import database
from app.db.repositories import extractions as extraction_repo
from app.db.repositories import llm_calls as llm_call_repo
from app.extraction.actions import extract_actions
from app.extraction.decisions import extract_decisions
from app.extraction.pipeline import ExtractionRun
from app.extraction.risks import extract_risks
from app.models.common import ExtractionType, ReviewStatus
from app.models.extraction import Extraction
from app.models.telemetry import UsageSummary

logger = logging.getLogger("agent.extract")

router = APIRouter(prefix="/api/extractions", tags=["extractions"])


@router.post("/{source_id}/actions", response_model=ExtractionRun, summary="M3, action items")
def run_action_extraction(source_id: str, replace_pending: bool = Query(default=True)) -> ExtractionRun:
    """Refuses with 403 when the source withheld consent.

    That check lives in the extraction pipeline, not here, so it holds however
    the run was triggered.
    """
    return extract_actions(source_id, get_settings(), replace_pending=replace_pending)


@router.post("/{source_id}/decisions", response_model=ExtractionRun, summary="M4, decisions")
def run_decision_extraction(source_id: str, replace_pending: bool = Query(default=True)) -> ExtractionRun:
    return extract_decisions(source_id, get_settings(), replace_pending=replace_pending)


@router.post("/{source_id}/risks", response_model=ExtractionRun, summary="M5, risks and blockers")
def run_risk_extraction(source_id: str, replace_pending: bool = Query(default=True)) -> ExtractionRun:
    return extract_risks(source_id, get_settings(), replace_pending=replace_pending)


@router.post("/{source_id}/all", response_model=list[ExtractionRun], summary="M3, M4 and M5 together")
def run_all_extractions(source_id: str, replace_pending: bool = Query(default=True)) -> list[ExtractionRun]:
    """Every extraction capability over one source, in one call.

    Each capability runs independently. One failing does not stop the others,
    because a rate limit part-way through should not cost the work already
    done, and each run reports its own failed chunks so a partial result is
    visible rather than silent.
    """
    settings = get_settings()
    runs: list[ExtractionRun] = []
    for extractor in (extract_actions, extract_decisions, extract_risks):
        try:
            runs.append(extractor(source_id, settings, replace_pending=replace_pending))
        except AgentError:
            raise
        except Exception as exc:  # one capability failing must not lose the others
            logger.warning("%s failed on %s: %s", extractor.__name__, source_id, exc)
    return runs


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
