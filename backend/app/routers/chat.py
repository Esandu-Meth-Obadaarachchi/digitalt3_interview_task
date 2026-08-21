"""HTTP surface for M9."""

from __future__ import annotations

from fastapi import APIRouter, Query

from app.config import get_settings
from app.db import database
from app.db.repositories import chat as chat_repo
from app.extraction.signals import SignalRun, classify_signals
from app.models.common import SignalClass

router = APIRouter(prefix="/api/chat", tags=["chat signals"])


@router.get("/messages", response_model=list[chat_repo.StoredMessage], summary="Stored messages")
def list_messages(
    source_id: str | None = Query(default=None),
    channel: str | None = Query(default=None),
    classification: SignalClass | None = Query(default=None),
    unclassified: bool | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1, le=500),
) -> list[chat_repo.StoredMessage]:
    """Never contains a direct message, and never contains noise once classified.

    Both are schema constraints rather than filters applied here: a DM cannot
    be stored at all, and 'noise' is not a storable classification.
    """
    with database.connect(get_settings()) as conn:
        return chat_repo.list_messages(
            conn,
            source_id=source_id,
            channel=channel,
            classification=classification,
            unclassified=unclassified,
            limit=limit,
        )


@router.get("/summary", summary="Counts by channel and class")
def summary(source_id: str | None = Query(default=None)) -> dict[str, object]:
    with database.connect(get_settings()) as conn:
        return {
            "by_class": chat_repo.counts_by_class(conn, source_id),
            "channels": chat_repo.channels(conn, source_id),
        }


@router.post("/{source_id}/classify", response_model=SignalRun, summary="Run M9 over an export")
def classify(source_id: str, replace_pending: bool = Query(default=True)) -> SignalRun:
    """Refuses with 403 when the source withheld consent.

    Noise is deleted rather than stored, and decision, blocker and request land
    in the same review queue as every other extraction.
    """
    return classify_signals(source_id, get_settings(), replace_pending=replace_pending)
