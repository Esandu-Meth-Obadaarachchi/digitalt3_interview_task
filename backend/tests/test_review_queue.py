"""M6 - the review and approval queue.

The database already refuses an approval with no reviewer and refuses to
reopen a terminal state. These tests cover the rules that live above it: what
a human is and is not allowed to wave through, and what the audit trail
records.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from app.db import database
from app.db.repositories import extractions as extraction_repo
from app.db.repositories import reviews as review_repo
from app.errors import NotFoundError, ReviewStateError
from app.extraction.actions import extract_actions
from app.extraction.llm.factory import set_provider_override
from app.extraction.llm.fake import FakeProvider
from app.ingestion.service import ingest_from_manifest
from app.models.common import ReviewStatus
from app.review import queue

SPRINT = "meeting-sprint-planning-2024-11-18"


@pytest.fixture()
def queued(settings, scripted_model):
    ingest_from_manifest(settings)
    scripted_model()
    extract_actions(SPRINT, settings)
    return settings


@pytest.fixture()
def unverified(settings, golden_actions):
    """A stored item whose quote could never be verified."""
    ingest_from_manifest(settings)
    real = next(g for g in golden_actions if g["source_id"] == SPRINT)
    stubborn = json.dumps({"actions": [{
        "what": real["what"], "owner": real["owner"], "due_date": real["due_date"],
        "verbatim_quote": "a quote that appears nowhere in this meeting",
        "speaker": real["speaker"], "timestamp": real["timestamp"], "confidence": 0.9}]})

    set_provider_override(FakeProvider().default(lambda request: stubborn))
    try:
        extract_actions(SPRINT, settings)
    finally:
        set_provider_override(None)

    with database.connect(settings) as conn:
        items = extraction_repo.list_extractions(conn, source_id=SPRINT, quote_verified=False)
    return settings, items[0]


def _first(settings):
    return queue.list_queue(settings, source_id=SPRINT)[0]


# --- the queue ---------------------------------------------------------------


def test_everything_extracted_starts_pending(queued):
    summary = queue.summary(queued, SPRINT)
    assert summary.pending == summary.total > 0
    assert summary.approved == summary.rejected == summary.expired == 0


def test_unverified_items_sort_to_the_top(unverified):
    settings, _ = unverified
    assert queue.list_queue(settings, source_id=SPRINT)[0].quote_verified is False


# --- editing -----------------------------------------------------------------


def test_an_edit_keeps_the_item_pending(queued):
    """An edit is not an approval. The corrected item must still be approved."""
    item = _first(queued)
    payload = dict(item.payload) | {"what": "Corrected by a human"}

    edited = queue.edit(item.id, payload, "esandu", "clarified the wording", queued)

    assert edited.status is ReviewStatus.PENDING
    assert edited.payload["what"] == "Corrected by a human"


def test_the_models_original_output_survives_every_edit(queued):
    item = _first(queued)
    original = item.original_payload["what"]

    for attempt in range(3):
        queue.edit(item.id, dict(item.payload) | {"what": f"edit {attempt}"}, "esandu", settings=queued)

    with database.connect(queued) as conn:
        stored = extraction_repo.get(conn, item.id)

    assert stored.original_payload["what"] == original
    assert stored.payload["what"] == "edit 2"


def test_an_edit_records_what_changed(queued):
    item = _first(queued)
    queue.edit(item.id, dict(item.payload) | {"owner": "Someone Else"}, "esandu", "wrong owner", queued)

    events = queue.history(item.id, queued)
    assert [e.event_type.value for e in events] == ["edited"]
    assert events[0].payload_before["owner"] == item.payload["owner"]
    assert events[0].payload_after["owner"] == "Someone Else"
    assert events[0].note == "wrong owner"


# --- approving and rejecting -------------------------------------------------


def test_approval_records_who_and_when(queued):
    item = _first(queued)
    approved = queue.approve(item.id, "esandu", settings=queued)

    assert approved.status is ReviewStatus.APPROVED
    assert approved.reviewer == "esandu"
    assert approved.reviewed_at


def test_approval_without_a_named_reviewer_is_refused(queued):
    item = _first(queued)
    with pytest.raises(ReviewStateError, match="name of the person"):
        queue.approve(item.id, "   ", settings=queued)


@pytest.mark.parametrize("action", ["approve", "reject"])
def test_a_terminal_item_cannot_be_reviewed_again(queued, action):
    item = _first(queued)
    queue.approve(item.id, "esandu", settings=queued)

    with pytest.raises(ReviewStateError, match="terminal"):
        getattr(queue, action)(item.id, "esandu", settings=queued)


def test_rejecting_leaves_it_unapprovable(queued):
    item = _first(queued)
    queue.reject(item.id, "esandu", "not a commitment", queued)

    with pytest.raises(ReviewStateError):
        queue.approve(item.id, "esandu", settings=queued)


def test_reviewing_something_that_does_not_exist_is_a_clean_error(queued):
    with pytest.raises(NotFoundError):
        queue.approve("no-such-extraction", "esandu", settings=queued)


# --- the unverified-quote override ------------------------------------------


def test_an_unverified_quote_cannot_be_approved_by_a_plain_click(unverified):
    settings, item = unverified
    with pytest.raises(ReviewStateError, match="could not be verified"):
        queue.approve(item.id, "esandu", settings=settings)


def test_overriding_without_a_reason_is_refused(unverified):
    settings, item = unverified
    with pytest.raises(ReviewStateError, match="written reason"):
        queue.approve(item.id, "esandu", override_unverified=True, settings=settings)


def test_an_override_is_allowed_and_is_marked_as_one_in_the_audit(unverified):
    settings, item = unverified
    approved = queue.approve(
        item.id, "esandu", "checked by hand against the recording",
        override_unverified=True, settings=settings,
    )

    assert approved.status is ReviewStatus.APPROVED
    event = queue.history(item.id, settings)[-1]
    assert event.note.startswith("OVERRIDE of unverified quote:")
    assert "checked by hand" in event.note


# --- the safe default on no response ----------------------------------------


def test_unreviewed_items_expire_and_expiry_is_not_approval(queued):
    """The rubric asks for a safe default on timeout. Refusal, never approval."""
    later = datetime.now(timezone.utc) + timedelta(hours=1000)
    expired = queue.expire_stale(queued, later)

    assert expired
    summary = queue.summary(queued, SPRINT)
    assert summary.pending == 0
    assert summary.expired == len(expired)
    assert summary.approved == 0


def test_an_expired_item_cannot_be_approved(queued):
    item = _first(queued)
    queue.expire_stale(queued, datetime.now(timezone.utc) + timedelta(hours=1000))

    with pytest.raises(ReviewStateError):
        queue.approve(item.id, "esandu", settings=queued)


def test_expiry_is_attributed_to_the_system_with_its_reason(queued):
    item = _first(queued)
    queue.expire_stale(queued, datetime.now(timezone.utc) + timedelta(hours=1000))

    event = queue.history(item.id, queued)[-1]
    assert event.event_type.value == "expired"
    assert event.actor == "system"
    assert "safe default is refusal" in event.note


def test_an_item_still_within_its_window_is_left_alone(queued):
    assert queue.expire_stale(queued, datetime.now(timezone.utc)) == []
    assert queue.summary(queued, SPRINT).pending > 0


# --- the audit trail ---------------------------------------------------------


def test_the_audit_trail_answers_who_approved_what_and_when(queued):
    items = queue.list_queue(queued, source_id=SPRINT)
    queue.approve(items[0].id, "esandu", settings=queued)
    queue.reject(items[1].id, "priya", "duplicate of the first", queued)

    with database.connect(queued) as conn:
        assert review_repo.by_actor(conn) == {"esandu": 1, "priya": 1}


def test_audit_events_cannot_be_altered_afterwards(queued):
    import sqlite3

    item = _first(queued)
    queue.approve(item.id, "esandu", settings=queued)
    event = queue.history(item.id, queued)[0]

    with pytest.raises((sqlite3.IntegrityError, Exception)):
        with database.transaction(queued) as conn:
            conn.execute("UPDATE review_events SET actor = 'someone else' WHERE id = ?", (event.id,))
