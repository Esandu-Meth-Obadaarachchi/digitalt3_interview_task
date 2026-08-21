"""M4 - decisions, and golden case 5.

    "Assert that the proposed-then-deferred item does not appear in the
     decision log. This tests whether your prompt distinguishes 'we decided'
     from 'we discussed'."

A negative test that cannot be made to fail proves nothing, so both directions
are covered: a model that omits the deferred item passes, and a model that
records it is caught.
"""

from __future__ import annotations

import pytest

from app.db import database
from app.db.repositories import extractions as extraction_repo
from app.errors import ConsentRefused, NotFoundError
from app.extraction.decisions import extract_decisions
from app.extraction.quote_verifier import verify_quote
from app.ingestion.normaliser import normalise_text
from app.ingestion.service import ingest_from_manifest
from app.models.common import UNSPECIFIED, ExtractionType, ReviewStatus

CLIENT = "meeting-client-status-2024-08-19"
SPRINT = "meeting-sprint-planning-2024-11-18"
HOTEL = "meeting-hotel-kickoff-2026-09-15"
NO_CONSENT = "meeting-team-sync-2024-11-15"


@pytest.fixture()
def ingested(settings):
    ingest_from_manifest(settings)
    return settings


def _stored(settings, source_id):
    with database.connect(settings) as conn:
        return extraction_repo.list_extractions(
            conn, source_id=source_id, extraction_type=ExtractionType.DECISION
        )


# --- the gate still applies --------------------------------------------------


def test_a_non_consented_source_is_never_sent_to_a_model(ingested, scripted_decision_model):
    provider = scripted_decision_model()

    with pytest.raises(ConsentRefused):
        extract_decisions(NO_CONSENT, ingested)

    assert provider.calls == []


def test_an_unknown_source_is_reported_not_crashed(ingested, scripted_decision_model):
    scripted_decision_model()
    with pytest.raises(NotFoundError):
        extract_decisions("no-such-source", ingested)


# --- the happy path ----------------------------------------------------------


def test_decisions_are_stored_as_pending_with_verified_quotes(ingested, scripted_decision_model):
    scripted_decision_model()
    run = extract_decisions(CLIENT, ingested)

    assert run.stored > 0
    assert run.unverified_quotes == 0

    stored = _stored(ingested, CLIENT)
    assert all(e.status is ReviewStatus.PENDING for e in stored)
    assert all(e.extraction_type is ExtractionType.DECISION for e in stored)
    assert all(e.quote_verified for e in stored)


def test_the_golden_decisions_are_found(ingested, scripted_decision_model, golden_decisions):
    scripted_decision_model()
    for source_id in (SPRINT, CLIENT):
        extract_decisions(source_id, ingested)

    expected = {
        normalise_text(d["verbatim_quote"])
        for d in golden_decisions["decisions"]
        if d["source_id"] in (SPRINT, CLIENT)
    }
    found = {
        normalise_text(e.verbatim_quote)
        for source_id in (SPRINT, CLIENT)
        for e in _stored(ingested, source_id)
    }
    assert expected <= found


def test_a_decision_payload_carries_rationale_and_alternatives(ingested, scripted_decision_model):
    scripted_decision_model()
    extract_decisions(SPRINT, ingested)

    stored = _stored(ingested, SPRINT)
    assert stored
    for extraction in stored:
        assert "what_was_decided" in extraction.payload
        assert "stated_rationale" in extraction.payload
        assert "who_stated_it" in extraction.payload
        assert isinstance(extraction.payload["alternatives_discussed"], list)


def test_an_unstated_rationale_is_unspecified_not_invented(ingested, scripted_decision_model):
    """A decision log full of tidy rationales reads better than one admitting
    nobody gave a reason, which is exactly why this has to be checked."""
    import json

    from app.extraction.llm.factory import set_provider_override
    from app.extraction.llm.fake import FakeProvider

    quote = "Okay, let's go with SSE for the real-time updates"
    set_provider_override(FakeProvider().default(lambda request: json.dumps({"decisions": (
        [{"what_was_decided": "Use SSE", "stated_rationale": "none given",
          "who_stated_it": "Sarah Chen", "alternatives_discussed": [],
          "verbatim_quote": quote, "timestamp": "00:03:32", "confidence": 0.9}]
        if normalise_text(quote) in normalise_text(request.prompt) else []
    )})))
    try:
        extract_decisions(SPRINT, ingested)
    finally:
        set_provider_override(None)

    stored = _stored(ingested, SPRINT)
    assert stored[0].payload["stated_rationale"] == UNSPECIFIED


# --- golden case 5, both directions -----------------------------------------


def test_the_deferred_decision_is_absent_when_the_model_behaves(
    ingested, scripted_decision_model, golden_decisions
):
    """Golden case 5, the passing direction."""
    scripted_decision_model(include_deferred=False)
    extract_decisions(CLIENT, ingested)

    deferred = [
        d for d in golden_decisions["deferred_decisions"] if d["source_id"] == CLIENT
    ]
    assert deferred, "the sample must contain a proposed-then-deferred item"

    quotes = {normalise_text(e.verbatim_quote) for e in _stored(ingested, CLIENT)}
    for item in deferred:
        assert normalise_text(item["verbatim_quote"]) not in quotes


def test_the_check_catches_a_model_that_records_a_deferred_item(
    ingested, scripted_decision_model, golden_decisions
):
    """Golden case 5, the failing direction.

    A negative test that cannot be made to fail proves nothing. Here the model
    is scripted to treat the deferral as a settled decision, and the assertion
    that would pass above must now fail.
    """
    scripted_decision_model(include_deferred=True)
    extract_decisions(CLIENT, ingested)

    deferred = next(d for d in golden_decisions["deferred_decisions"] if d["source_id"] == CLIENT)
    quotes = {normalise_text(e.verbatim_quote) for e in _stored(ingested, CLIENT)}

    assert normalise_text(deferred["verbatim_quote"]) in quotes, (
        "the scripted failure did not reach the store, so this test is not "
        "exercising what it claims to exercise"
    )


def test_the_hotel_transcript_defers_a_naming_decision(
    ingested, scripted_decision_model, golden_decisions
):
    """A second instance of the same trap, in a transcript written independently
    of this build. The permanent product name is deferred while a working name
    is settled in the same breath: the working name IS a decision."""
    deferred = [d for d in golden_decisions["deferred_decisions"] if d["source_id"] == HOTEL]
    assert deferred, "the hotel kickoff must carry its own deferred decision"

    scripted_decision_model(include_deferred=False)
    extract_decisions(HOTEL, ingested)

    quotes = {normalise_text(e.verbatim_quote) for e in _stored(ingested, HOTEL)}
    for item in deferred:
        assert normalise_text(item["verbatim_quote"]) not in quotes


# --- quotes and deduplication ------------------------------------------------


def test_every_stored_decision_quote_is_a_literal_substring(ingested, scripted_decision_model):
    from app.db.repositories import segments as segment_repo

    scripted_decision_model()
    extract_decisions(CLIENT, ingested)

    with database.connect(ingested) as conn:
        source_text = segment_repo.get_source_text(conn, CLIENT)

    for extraction in _stored(ingested, CLIENT):
        assert verify_quote(extraction.verbatim_quote, source_text), extraction.id


def test_a_recap_restating_a_decision_is_deduplicated(ingested):
    """The hotel kickoff ends by restating every architectural decision. The
    same person settling the same question twice is one decision."""
    import json

    from app.extraction.llm.factory import set_provider_override
    from app.extraction.llm.fake import FakeProvider

    settled = "Agreed. PostgreSQL for the main database. Let's use SQLAlchemy as the ORM with Alembic for migrations"
    recap = "FastAPI backend, Next.js frontend, PostgreSQL database, Docker for everything"

    def respond(request):
        chunk = normalise_text(request.prompt)
        out = []
        for quote, ts in ((settled, "00:01:44"), (recap, "00:08:32")):
            if normalise_text(quote) in chunk:
                out.append({
                    "what_was_decided": "Use PostgreSQL as the main database",
                    "stated_rationale": "UNSPECIFIED", "who_stated_it": "Ranidu",
                    "alternatives_discussed": [], "verbatim_quote": quote,
                    "timestamp": ts, "confidence": 0.9,
                })
        return json.dumps({"decisions": out})

    set_provider_override(FakeProvider().default(respond))
    try:
        run = extract_decisions(HOTEL, ingested)
    finally:
        set_provider_override(None)

    assert run.duplicates_removed >= 1
    postgres = [e for e in _stored(ingested, HOTEL) if "PostgreSQL" in e.payload["what_was_decided"]]
    assert len(postgres) == 1
