"""M3 end to end: extraction, quote verification, deduplication, storage."""

from __future__ import annotations

import json

import pytest

from app.db import database
from app.db.repositories import extractions as extraction_repo
from app.errors import ConsentRefused, NotFoundError
from app.extraction.actions import extract_actions
from app.extraction.deduplicator import Candidate, deduplicate, task_similarity
from app.extraction.llm.factory import set_provider_override
from app.extraction.llm.fake import FakeProvider
from app.extraction.quote_verifier import verify_quote
from app.ingestion.service import ingest_from_manifest
from app.models.common import UNSPECIFIED, ExtractionType, ReviewStatus
from app.models.extraction import QuoteLocation

SPRINT = "meeting-sprint-planning-2024-11-18"
NO_CONSENT = "meeting-team-sync-2024-11-15"
MALFORMED = "meeting-design-review-2024-11-17"


@pytest.fixture()
def ingested(settings):
    ingest_from_manifest(settings)
    return settings


# --- M2, the last gate before the transcript leaves the process --------------


def test_a_non_consented_source_is_never_sent_to_a_model(ingested, scripted_model):
    provider = scripted_model()

    with pytest.raises(ConsentRefused):
        extract_actions(NO_CONSENT, ingested)

    assert provider.calls == [], "not one request may reach the provider"


def test_a_rejected_source_has_no_segments_to_extract_from(ingested, scripted_model):
    scripted_model()
    with pytest.raises(NotFoundError, match="has status 'error'"):
        extract_actions(MALFORMED, ingested)


def test_an_unknown_source_is_reported_not_crashed(ingested, scripted_model):
    scripted_model()
    with pytest.raises(NotFoundError):
        extract_actions("no-such-source", ingested)


# --- the happy path ----------------------------------------------------------


def test_extraction_stores_verified_pending_actions(ingested, scripted_model):
    scripted_model()
    run = extract_actions(SPRINT, ingested)

    assert run.chunks > 1
    assert run.stored > 0
    assert run.unverified_quotes == 0
    assert run.verified_quotes == run.stored

    with database.connect(ingested) as conn:
        stored = extraction_repo.list_extractions(conn, source_id=SPRINT)

    assert all(e.status is ReviewStatus.PENDING for e in stored)
    assert all(e.extraction_type is ExtractionType.ACTION for e in stored)
    assert all(e.quote_verified for e in stored)
    assert all(e.prompt_version and e.model_name for e in stored), "provenance must be recorded"


def test_every_stored_quote_is_a_literal_substring_of_the_transcript(ingested, scripted_model):
    """Golden case 2, checked directly rather than through the stored flag."""
    from app.db.repositories import segments as segment_repo

    scripted_model()
    extract_actions(SPRINT, ingested)

    with database.connect(ingested) as conn:
        source_text = segment_repo.get_source_text(conn, SPRINT)
        stored = extraction_repo.list_extractions(conn, source_id=SPRINT)

    for extraction in stored:
        assert verify_quote(extraction.verbatim_quote, source_text), extraction.id


def test_every_stored_quote_span_resolves_to_the_quote(ingested, scripted_model):
    """A citation must point at a location inside the source, not at the source."""
    from app.db.repositories import segments as segment_repo

    scripted_model()
    extract_actions(SPRINT, ingested)

    with database.connect(ingested) as conn:
        source_text = segment_repo.get_source_text(conn, SPRINT)
        stored = extraction_repo.list_extractions(conn, source_id=SPRINT)

    from app.ingestion.normaliser import normalise_text

    for extraction in stored:
        location = extraction.quote_location
        assert location is not None
        assert source_text[location.char_start : location.char_end] == normalise_text(
            extraction.verbatim_quote
        )


def test_chunk_overlap_duplicates_are_removed(ingested, scripted_model):
    scripted_model()
    run = extract_actions(SPRINT, ingested)

    assert run.duplicates_removed > 0, "overlap should produce duplicates to remove"
    assert run.stored == run.candidates - run.duplicates_removed

    with database.connect(ingested) as conn:
        stored = extraction_repo.list_extractions(conn, source_id=SPRINT)

    keys = [e.dedup_key for e in stored]
    assert len(keys) == len(set(keys)), "no two stored items share a dedup key"


def test_an_absorbed_duplicate_is_recorded_on_its_survivor(ingested, scripted_model):
    scripted_model()
    extract_actions(SPRINT, ingested)

    with database.connect(ingested) as conn:
        stored = extraction_repo.list_extractions(conn, source_id=SPRINT)
        merged = [e for e in stored if e.merged_from]
        reasons = conn.execute(
            "SELECT merge_reason FROM extractions WHERE merge_reason IS NOT NULL"
        ).fetchall()

    assert merged, "at least one survivor should carry what it absorbed"
    assert reasons and all(r["merge_reason"] for r in reasons)


# --- honest abstention -------------------------------------------------------


def test_the_planted_ownerless_commitments_stay_unspecified(ingested, scripted_model, golden_actions):
    """Golden case 3b. A guessed owner is a failure, not a near-miss."""
    scripted_model()
    extract_actions(SPRINT, ingested)

    expected = {g["verbatim_quote"] for g in golden_actions
                if g["source_id"] == SPRINT and g["owner"] == UNSPECIFIED}
    assert len(expected) == 2, "the sample plants exactly two ownerless commitments"

    with database.connect(ingested) as conn:
        stored = extraction_repo.list_extractions(conn, source_id=SPRINT)

    from app.ingestion.normaliser import normalise_text

    for quote in expected:
        match = next(e for e in stored if normalise_text(e.verbatim_quote) == normalise_text(quote))
        assert match.payload["owner"] == UNSPECIFIED


def test_a_relative_date_is_resolved_and_its_rule_recorded(ingested, scripted_model):
    scripted_model()
    extract_actions(SPRINT, ingested)

    with database.connect(ingested) as conn:
        stored = extraction_repo.list_extractions(conn, source_id=SPRINT)

    resolved = [e for e in stored if e.payload.get("due_date_type") == "relative_resolved"]
    assert resolved

    for extraction in resolved:
        assert extraction.payload["due_date"] != UNSPECIFIED
        assert extraction.payload["due_date_stated"]
        assert extraction.payload["due_date_rule"]


def test_no_concrete_date_appears_without_stated_words_behind_it(ingested, scripted_model):
    """Golden case 4, as a structural property rather than a count."""
    scripted_model()
    extract_actions(SPRINT, ingested)

    with database.connect(ingested) as conn:
        stored = extraction_repo.list_extractions(conn, source_id=SPRINT)

    for extraction in stored:
        if extraction.payload["due_date"] != UNSPECIFIED:
            assert extraction.payload["due_date_stated"], extraction.id


# --- when the model misbehaves ----------------------------------------------


def test_a_fabricated_quote_triggers_a_repair_and_the_repair_is_accepted(ingested, golden_actions):
    """The model invents a quote once, then corrects itself."""
    real = next(g for g in golden_actions if g["source_id"] == SPRINT)
    fabricated = json.dumps({"actions": [{
        "what": real["what"], "owner": real["owner"], "due_date": real["due_date"],
        "verbatim_quote": "I promise to deliver everything ahead of schedule",
        "speaker": real["speaker"], "timestamp": real["timestamp"], "confidence": 0.9}]})
    corrected = json.dumps({"actions": [{
        "what": real["what"], "owner": real["owner"], "due_date": real["due_date"],
        "verbatim_quote": real["verbatim_quote"],
        "speaker": real["speaker"], "timestamp": real["timestamp"], "confidence": 0.9}]})

    provider = FakeProvider().queue(fabricated, corrected).default(lambda r: '{"actions": []}')
    set_provider_override(provider)
    try:
        run = extract_actions(SPRINT, ingested)
    finally:
        set_provider_override(None)

    assert run.unverified_quotes == 0

    repair = provider.calls[1].prompt
    assert "YOUR PREVIOUS RESPONSE WAS REJECTED" in repair
    assert "ahead of schedule" in repair, "the model is told which quote failed"
    assert "Copy the words exactly" in repair

    with database.connect(ingested) as conn:
        stored = extraction_repo.list_extractions(conn, source_id=SPRINT)
    assert all(e.quote_verified for e in stored)
    assert not any("ahead of schedule" in e.verbatim_quote for e in stored)


def test_a_quote_the_model_will_not_fix_is_flagged_rather_than_discarded(ingested, golden_actions):
    """Discarding it would make the fabricated-quote metric zero by construction.

    Kept, flagged, sorted to the top of the queue, and unapprovable without an
    explicit override.
    """
    real = next(g for g in golden_actions if g["source_id"] == SPRINT)
    stubborn = json.dumps({"actions": [{
        "what": real["what"], "owner": real["owner"], "due_date": real["due_date"],
        "verbatim_quote": "a quote that appears nowhere in this meeting",
        "speaker": real["speaker"], "timestamp": real["timestamp"], "confidence": 0.9}]})

    set_provider_override(FakeProvider().default(lambda request: stubborn))
    try:
        run = extract_actions(SPRINT, ingested)
    finally:
        set_provider_override(None)

    assert run.unverified_quotes > 0
    assert run.stored > 0

    with database.connect(ingested) as conn:
        stored = extraction_repo.list_extractions(conn, source_id=SPRINT)

    assert stored[0].quote_verified is False, "unverified items sort to the top of the queue"
    assert all(e.needs_override_to_approve for e in stored if not e.quote_verified)


def test_one_fabricated_quote_does_not_poison_its_neighbours(ingested, golden_actions):
    """The bug a live demo found: quote_verified was set per chunk rather than
    per item. When one action in a model's response had a fabricated quote,
    the retry budget exhausted and the whole response was accepted, and every
    item from it - including ones whose own quote genuinely located in the
    source - was marked unverified.

    A decision quoting "End of October for Phase 1 complete..." verbatim was
    flagged unverified, sent to the queue demanding an override for a quote
    that was never in question, purely because a sibling in the same response
    had failed.
    """
    real = next(g for g in golden_actions if g["source_id"] == SPRINT)
    batch_with_one_bad_apple = json.dumps({"actions": [
        {
            "what": real["what"], "owner": real["owner"], "due_date": real["due_date"],
            "verbatim_quote": real["verbatim_quote"],
            "speaker": real["speaker"], "timestamp": real["timestamp"], "confidence": 0.9,
        },
        {
            "what": "Something nobody said", "owner": UNSPECIFIED, "due_date": UNSPECIFIED,
            "verbatim_quote": "a quote that appears nowhere in this meeting",
            "speaker": real["speaker"], "timestamp": real["timestamp"], "confidence": 0.9,
        },
    ]})

    set_provider_override(FakeProvider().default(lambda request: batch_with_one_bad_apple))
    try:
        extract_actions(SPRINT, ingested)
    finally:
        set_provider_override(None)

    with database.connect(ingested) as conn:
        stored = extraction_repo.list_extractions(conn, source_id=SPRINT)

    genuine = next(e for e in stored if e.verbatim_quote == real["verbatim_quote"])
    fabricated = next(e for e in stored if "appears nowhere" in e.verbatim_quote)

    assert genuine.quote_verified is True, (
        "a quote that independently locates in the source must not be marked "
        "unverified because another item in the same response was fabricated"
    )
    assert genuine.needs_override_to_approve is False
    assert fabricated.quote_verified is False
    assert fabricated.needs_override_to_approve is True


def test_a_model_returning_nothing_is_a_valid_outcome(ingested):
    """A transcript with no commitments must produce no actions, not a crash."""
    set_provider_override(FakeProvider())
    try:
        run = extract_actions(SPRINT, ingested)
    finally:
        set_provider_override(None)

    assert run.stored == 0
    assert run.failed_chunks == []
    assert run.ok


def test_rerunning_replaces_pending_rather_than_duplicating(ingested, scripted_model):
    scripted_model()
    first = extract_actions(SPRINT, ingested)
    second = extract_actions(SPRINT, ingested)

    assert first.stored == second.stored
    with database.connect(ingested) as conn:
        assert len(extraction_repo.list_extractions(conn, source_id=SPRINT)) == second.stored


# --- deduplication rules -----------------------------------------------------


def _candidate(key, quote, task, confidence=0.8, start=0, end=50):
    return Candidate(key=key, quote=quote, task=task, confidence=confidence,
                     location=QuoteLocation(char_start=start, char_end=end))


def test_two_commitments_in_one_sentence_are_not_merged():
    """Region alone is not enough. The model quotes the same sentence for both."""
    survivors, _ = deduplicate([
        _candidate("a", "I'll write the tests and Priya will review the schema", "Write the aggregation tests"),
        _candidate("b", "I'll write the tests and Priya will review the schema", "Review the database schema changes"),
    ])
    assert len(survivors) == 2


def test_the_same_commitment_worded_differently_is_merged():
    survivors, merges = deduplicate([
        _candidate("a", "I can have the refactor done with tests by Friday",
                   "Finish auth refactor with integration tests", 0.9, 100, 148),
        _candidate("b", "have the refactor done with tests by Friday",
                   "Complete the authentication refactor and its tests", 0.7, 106, 148),
    ])
    assert [c.key for c in survivors] == ["a"]
    assert merges[0].absorbed_keys == ["b"]
    assert "overlap" in merges[0].reason


def test_the_survivor_is_the_higher_confidence_candidate():
    survivors, _ = deduplicate([
        _candidate("low", "same quote here", "same task words entirely", 0.3),
        _candidate("high", "same quote here", "same task words entirely", 0.95),
    ])
    assert [c.key for c in survivors] == ["high"]


def test_similar_work_in_different_parts_of_a_meeting_is_not_merged():
    """Task alone is not enough either."""
    survivors, _ = deduplicate([
        _candidate("a", "Sarah will write the migration tests", "Write the migration tests", 0.8, 0, 40),
        _candidate("b", "James will write the migration tests", "Write the migration tests", 0.8, 900, 940),
    ])
    assert len(survivors) == 2


def test_containment_is_used_rather_than_jaccard():
    """Jaccard punishes the model for describing one task at two lengths."""
    long_form = "Finish the authentication module refactor with full integration tests"
    short_form = "Finish auth refactor"
    assert task_similarity(long_form, short_form) >= 0.4


def test_an_unverified_item_is_not_absorbed_into_a_verified_one():
    """It has no location, so it can only match on an identical quote."""
    unverified = Candidate(key="u", quote="invented text", task="Write the tests", confidence=0.9, location=None)
    verified = _candidate("v", "real text from the transcript", "Write the tests", 0.8, 0, 29)
    survivors, _ = deduplicate([verified, unverified])
    assert len(survivors) == 2
