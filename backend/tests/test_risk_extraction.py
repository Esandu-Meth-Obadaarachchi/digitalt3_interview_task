"""M5 - risks and blockers.

    "Both golden risks are found and severity is defensible from the quote
     alone."

Defensibility is the unusual part. It is not a question of whether the severity
is right in the abstract, but of whether somebody holding only the quote could
see why that level was chosen. That is a property of the stored record, and it
is asserted here as one.
"""

from __future__ import annotations

import pytest

from app.db import database
from app.db.repositories import extractions as extraction_repo
from app.db.repositories import segments as segment_repo
from app.errors import ConsentRefused
from app.extraction.risks import extract_risks
from app.extraction.quote_verifier import verify_quote
from app.ingestion.normaliser import normalise_text
from app.ingestion.service import ingest_from_manifest
from app.models.common import UNSPECIFIED, ExtractionType, ReviewStatus, Severity

SPRINT = "meeting-sprint-planning-2024-11-18"
CLIENT = "meeting-client-status-2024-08-19"
HOTEL = "meeting-hotel-kickoff-2026-09-15"
NO_CONSENT = "meeting-team-sync-2024-11-15"


@pytest.fixture()
def ingested(settings):
    ingest_from_manifest(settings)
    return settings


def _stored(settings, source_id):
    with database.connect(settings) as conn:
        return extraction_repo.list_extractions(
            conn, source_id=source_id, extraction_type=ExtractionType.RISK
        )


# --- the gate still applies --------------------------------------------------


def test_a_non_consented_source_is_never_sent_to_a_model(ingested, scripted_risk_model):
    provider = scripted_risk_model()

    with pytest.raises(ConsentRefused):
        extract_risks(NO_CONSENT, ingested)

    assert provider.calls == []


# --- both golden risks are found ---------------------------------------------


def test_both_golden_risks_are_found(ingested, scripted_risk_model, golden_risks):
    scripted_risk_model()
    for source_id in (SPRINT, CLIENT):
        extract_risks(source_id, ingested)

    expected = {
        normalise_text(r["verbatim_quote"])
        for r in golden_risks
        if r["source_id"] in (SPRINT, CLIENT)
    }
    assert len(expected) == 2, "the original sample carries exactly two golden risks"

    found = {
        normalise_text(e.verbatim_quote)
        for source_id in (SPRINT, CLIENT)
        for e in _stored(ingested, source_id)
    }
    assert expected <= found


def test_risks_are_stored_as_pending_with_verified_quotes(ingested, scripted_risk_model):
    scripted_risk_model()
    run = extract_risks(SPRINT, ingested)

    assert run.stored > 0
    assert run.unverified_quotes == 0

    for extraction in _stored(ingested, SPRINT):
        assert extraction.status is ReviewStatus.PENDING
        assert extraction.extraction_type is ExtractionType.RISK
        assert extraction.quote_verified


def test_every_stored_risk_quote_is_a_literal_substring(ingested, scripted_risk_model):
    scripted_risk_model()
    extract_risks(SPRINT, ingested)

    with database.connect(ingested) as conn:
        source_text = segment_repo.get_source_text(conn, SPRINT)

    for extraction in _stored(ingested, SPRINT):
        assert verify_quote(extraction.verbatim_quote, source_text), extraction.id


# --- severity ----------------------------------------------------------------


def test_severity_is_one_of_three_named_bands(ingested, scripted_risk_model):
    """Named bands rather than a score, because a reviewer can argue with
    "high" in a way they cannot argue with 0.72."""
    scripted_risk_model()
    extract_risks(SPRINT, ingested)

    for extraction in _stored(ingested, SPRINT):
        assert extraction.payload["severity"] in {s.value for s in Severity}


def test_the_quote_carries_the_consequence_the_severity_rests_on(
    ingested, scripted_risk_model, golden_risks
):
    """Defensibility, asserted as a property of the record.

    A high severity has to be readable from the quote alone. Both golden risks
    are labelled high, and both quotes state what would happen: missing an
    integration deadline, and delaying go-live by a month.
    """
    scripted_risk_model()
    for source_id in (SPRINT, CLIENT):
        extract_risks(source_id, ingested)

    consequence_words = (
        "miss", "missed", "delay", "postpone", "lose", "lost", "trust",
        "deadline", "blocked", "cannot", "can't", "fail", "risk",
    )

    high = [
        e
        for source_id in (SPRINT, CLIENT)
        for e in _stored(ingested, source_id)
        if e.payload["severity"] == "high"
    ]
    assert high, "both golden risks are high severity"

    for extraction in high:
        quote = extraction.verbatim_quote.lower()
        assert any(word in quote for word in consequence_words), (
            f"a high severity must be defensible from the quote alone, and "
            f"{extraction.id} quotes no consequence: {extraction.verbatim_quote!r}"
        )


def test_an_invalid_severity_is_rejected_by_the_contract(ingested):
    """The model cannot invent a fourth band. A schema violation triggers the
    repair loop rather than storing something nothing downstream understands."""
    import json

    from app.extraction.llm.factory import set_provider_override
    from app.extraction.llm.fake import FakeProvider

    bad = json.dumps({"risks": [{
        "description": "x", "severity": "catastrophic", "affected_area": "y", "owner": "z",
        "verbatim_quote": "q", "speaker": "s", "timestamp": "00:00:01", "confidence": 0.5,
    }]})
    provider = FakeProvider().queue(bad).default(lambda request: '{"risks": []}')
    set_provider_override(provider)
    try:
        extract_risks(SPRINT, ingested)
    finally:
        set_provider_override(None)

    assert "severity" in provider.calls[1].prompt


# --- ownership ---------------------------------------------------------------


def test_raising_a_risk_is_not_owning_it(ingested, scripted_risk_model, golden_risks):
    """The sprint planning risk is raised by Sarah Chen with nobody named to
    own it. Attributing it to the speaker would be an invented owner."""
    scripted_risk_model()
    extract_risks(SPRINT, ingested)

    expected = next(
        r for r in golden_risks
        if r["source_id"] == SPRINT and r["owner"] == UNSPECIFIED
    )
    stored = next(
        e for e in _stored(ingested, SPRINT)
        if normalise_text(e.verbatim_quote) == normalise_text(expected["verbatim_quote"])
    )

    assert stored.payload["owner"] == UNSPECIFIED
    assert stored.speaker == expected["speaker"], "the speaker is still recorded"


def test_an_unstated_affected_area_is_unspecified(ingested, scripted_risk_model):
    scripted_risk_model(transform=lambda risks: [r | {"affected_area": "not stated"} for r in risks])
    extract_risks(SPRINT, ingested)

    stored = _stored(ingested, SPRINT)
    assert stored
    assert all(e.payload["affected_area"] == UNSPECIFIED for e in stored)


# --- deduplication is deliberately different for risks -----------------------


def test_two_risks_raised_by_one_person_are_not_merged(ingested):
    """Deliberately unlike actions and decisions.

    The cross-region rule merges the same named person committing to the same
    thing twice. For risks that is wrong: the person named on two risks is
    usually the person who noticed both. Merging would silently discard a real
    concern, and a lost risk is worse than a duplicate one a reviewer can
    dismiss in a click.
    """
    import json

    from app.extraction.llm.factory import set_provider_override
    from app.extraction.llm.fake import FakeProvider

    first = "Booking.com and Expedia don't have public APIs for rate data"
    second = "That's four AI features plus six core modules. That's a massive scope"

    def respond(request):
        chunk = normalise_text(request.prompt)
        out = []
        for quote, ts in ((first, "00:07:20"), (second, "00:04:38")):
            if normalise_text(quote) in chunk:
                out.append({
                    "description": "A scoping or integration concern raised by Haritha",
                    "severity": "medium", "affected_area": "UNSPECIFIED", "owner": "Haritha",
                    "verbatim_quote": quote, "speaker": "Haritha",
                    "timestamp": ts, "confidence": 0.8,
                })
        return json.dumps({"risks": out})

    set_provider_override(FakeProvider().default(respond))
    try:
        extract_risks(HOTEL, ingested)
    finally:
        set_provider_override(None)

    quotes = {normalise_text(e.verbatim_quote) for e in _stored(ingested, HOTEL)}
    assert normalise_text(first) in quotes
    assert normalise_text(second) in quotes, (
        "identical description and owner, different concerns. Merging these "
        "would lose a real risk."
    )
