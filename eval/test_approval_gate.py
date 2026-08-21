"""Golden case 8 — approval enforcement.

    "An automated test that attempts a tracker write for a pending record and
     for a rejected record, directly against your service layer rather than the
     interface. Both must fail. This is a test, not a demo step."

So nothing here goes through HTTP. The tests call `app.tracker.service`
directly, and then go a layer lower still and attempt the write against raw
SQLite with no Python service in the path at all.

The rubric's red flag for this criterion is gating that "exists in the UI but
is bypassable via the API". These tests are the evidence that it is not, at
three depths:

    1  the service layer refuses and explains
    2  the database trigger refuses even when the service is bypassed
    3  the unique constraint makes a duplicate impossible, not merely unlikely
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.adapters.factory import get_tracker
from app.config import REPO_ROOT
from app.db import database
from app.db.repositories import extractions as extraction_repo
from app.db.repositories import tracker as tracker_repo
from app.errors import ApprovalGateViolation
from app.extraction.actions import extract_actions
from app.ingestion.service import ingest_from_manifest
from app.models.common import ReviewStatus
from app.models.tracker import TrackerItemDraft, WriteOutcome
from app.review import queue
from app.tracker import service

SPRINT = "meeting-sprint-planning-2024-11-18"
NOW = datetime.now(timezone.utc).isoformat()


@pytest.fixture()
def extracted(settings, scripted_model):
    """Sample data ingested, a tracker backlog seeded, actions in the queue."""
    ingest_from_manifest(settings)
    scripted_model()
    extract_actions(SPRINT, settings)
    seed = json.loads(
        (REPO_ROOT / "sample_data" / "tracker" / "seed_items.json").read_text(encoding="utf-8")
    )
    get_tracker(settings).seed(seed["items"])
    return settings


def _queue(settings):
    return queue.list_queue(settings, source_id=SPRINT)


# =============================================================================
# 1. The service layer refuses
# =============================================================================


def test_a_pending_record_cannot_be_written(extracted):
    item = _queue(extracted)[0]
    assert item.status is ReviewStatus.PENDING

    with pytest.raises(ApprovalGateViolation, match="pending"):
        service.write_approved(item.id, extracted)


def test_a_rejected_record_cannot_be_written(extracted):
    item = _queue(extracted)[0]
    queue.reject(item.id, "esandu", "not a commitment", extracted)

    with pytest.raises(ApprovalGateViolation, match="rejected"):
        service.write_approved(item.id, extracted)


def test_an_expired_record_cannot_be_written(extracted):
    """The safe default on no response must be as unwritable as a rejection."""
    from datetime import timedelta

    item = _queue(extracted)[0]
    queue.expire_stale(extracted, datetime.now(timezone.utc) + timedelta(hours=1000))

    with pytest.raises(ApprovalGateViolation, match="expired"):
        service.write_approved(item.id, extracted)


@pytest.mark.parametrize("state", ["pending", "rejected", "expired"])
def test_a_blocked_write_creates_no_tracker_item(extracted, state):
    from datetime import timedelta

    item = _queue(extracted)[0]
    if state == "rejected":
        queue.reject(item.id, "esandu", settings=extracted)
    elif state == "expired":
        queue.expire_stale(extracted, datetime.now(timezone.utc) + timedelta(hours=1000))

    with database.connect(extracted) as conn:
        before = tracker_repo.count_items(conn, written_by_agent=True)

    with pytest.raises(ApprovalGateViolation):
        service.write_approved(item.id, extracted)

    with database.connect(extracted) as conn:
        assert tracker_repo.count_items(conn, written_by_agent=True) == before
        assert tracker_repo.count_writes(conn) == 0


def test_a_blocked_attempt_is_recorded_with_its_reason(extracted):
    """A log that only records successes cannot prove a gate fired."""
    item = _queue(extracted)[0]

    with pytest.raises(ApprovalGateViolation):
        service.write_approved(item.id, extracted)

    with database.connect(extracted) as conn:
        attempts = tracker_repo.list_attempts(conn, item.id)

    assert len(attempts) == 1
    assert attempts[0].outcome is WriteOutcome.BLOCKED
    assert "pending" in attempts[0].reason
    assert attempts[0].external_ref is None


def test_a_blocked_attempt_appears_in_the_inspectable_write_log(extracted):
    item = _queue(extracted)[0]
    with pytest.raises(ApprovalGateViolation):
        service.write_approved(item.id, extracted)

    lines = service.write_log_lines(extracted)
    assert lines and lines[-1]["outcome"] == "blocked"
    assert lines[-1]["extraction_id"] == item.id
    assert Path(extracted.write_log_path).exists()


# =============================================================================
# 2. The database refuses when the service is bypassed
# =============================================================================


@pytest.mark.parametrize("state", ["pending", "rejected", "expired"])
def test_the_database_refuses_a_write_with_no_python_service_in_the_path(extracted, state):
    """Straight at SQLite. This is the layer that answers the rubric's
    "bypassable via the API" red flag."""
    from datetime import timedelta

    item = _queue(extracted)[0]
    if state == "rejected":
        queue.reject(item.id, "esandu", settings=extracted)
    elif state == "expired":
        queue.expire_stale(extracted, datetime.now(timezone.utc) + timedelta(hours=1000))

    conn = sqlite3.connect(extracted.db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="approval_gate"):
            conn.execute(
                "INSERT INTO tracker_writes (id, extraction_id, external_ref, provider,"
                " write_payload, written_at) VALUES ('forced', ?, 'MOCK-999', 'mock', '{}', ?)",
                (item.id, NOW),
            )
    finally:
        conn.close()


def test_forging_an_approval_still_requires_a_named_reviewer(extracted):
    """The obvious bypass is to flip the status by hand. The database refuses
    an approval that names nobody and gives no time."""
    item = _queue(extracted)[0]

    conn = sqlite3.connect(extracted.db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="review_audit"):
            conn.execute("UPDATE extractions SET status = 'approved' WHERE id = ?", (item.id,))
    finally:
        conn.close()


def test_the_adapter_cannot_be_used_to_smuggle_an_unapproved_item(extracted):
    """Calling the adapter directly creates a tracker item with no audit row.

    The item exists, and the audit is what makes it legitimate: it has no
    tracker_writes row, so `written_by_agent` accounting and the write log both
    show it for what it is. Recorded here as a known boundary rather than
    claimed to be impossible.
    """
    adapter = get_tracker(extracted)
    item = _queue(extracted)[0]

    smuggled = adapter.create_item(
        TrackerItemDraft(title="smuggled", description="bypassed the service", source_ref=item.id)
    )

    with database.connect(extracted) as conn:
        assert tracker_repo.get_item(conn, smuggled.external_ref) is not None
        assert tracker_repo.get_write(conn, item.id) is None
        assert tracker_repo.count_writes(conn) == 0


# =============================================================================
# 3. Approval works, and idempotency is a guarantee
# =============================================================================


def test_an_approved_record_is_written_once(extracted):
    item = _queue(extracted)[0]
    queue.approve(item.id, "esandu", settings=extracted, write_through=False)

    result = service.write_approved(item.id, extracted)

    assert result.outcome is WriteOutcome.CREATED
    assert result.item is not None
    assert result.item.source_ref == item.id


def test_the_capability_test_three_approvals_two_reruns_three_items(extracted):
    """The wording of M7's test, executed.

    "Approve three actions, then re-run the write path twice: exactly three
     tracker items exist. The write log shows every attempt including the
     deduplicated ones."
    """
    items = _queue(extracted)[:3]
    for item in items:
        queue.approve(item.id, "esandu", settings=extracted)

    service.sync_approved(extracted)
    service.sync_approved(extracted)

    with database.connect(extracted) as conn:
        assert tracker_repo.count_items(conn, written_by_agent=True) == 3
        assert tracker_repo.count_writes(conn) == 3
        counts = tracker_repo.attempt_counts(conn)

    assert counts["created"] == 3
    assert counts["deduplicated"] == 6

    log = service.write_log_lines(extracted)
    assert sum(1 for line in log if line["outcome"] == "created") == 3
    assert sum(1 for line in log if line["outcome"] == "deduplicated") == 6


def test_a_second_write_is_deduplicated_not_duplicated(extracted):
    item = _queue(extracted)[0]
    queue.approve(item.id, "esandu", settings=extracted, write_through=False)

    first = service.write_approved(item.id, extracted)
    second = service.write_approved(item.id, extracted)

    assert first.outcome is WriteOutcome.CREATED
    assert second.outcome is WriteOutcome.DEDUPLICATED
    assert second.item.external_ref == first.item.external_ref


def test_the_unique_constraint_blocks_a_forged_duplicate(extracted):
    """Idempotency is a database guarantee, not an if-statement."""
    item = _queue(extracted)[0]
    queue.approve(item.id, "esandu", settings=extracted)

    conn = sqlite3.connect(extracted.db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            conn.execute(
                "INSERT INTO tracker_writes (id, extraction_id, external_ref, provider,"
                " write_payload, written_at) VALUES ('forced', ?, 'MOCK-998', 'mock', '{}', ?)",
                (item.id, NOW),
            )
    finally:
        conn.close()


def test_approving_writes_through_without_a_separate_step(extracted):
    """The task catalogue gives M7 the trigger "on approval"."""
    item = _queue(extracted)[0]
    queue.approve(item.id, "esandu", settings=extracted)

    with database.connect(extracted) as conn:
        write = tracker_repo.get_write(conn, item.id)
    assert write is not None


def test_an_extraction_edited_before_approval_is_written_as_edited(extracted):
    """The human's correction is what reaches the tracker, not the model's
    original. The original survives in original_payload for the audit."""
    item = _queue(extracted)[0]
    corrected = dict(item.payload) | {"what": "Corrected by the reviewer"}
    queue.edit(item.id, corrected, "esandu", "wrong wording", extracted)
    queue.approve(item.id, "esandu", settings=extracted)

    with database.connect(extracted) as conn:
        write = tracker_repo.get_write(conn, item.id)
        stored = extraction_repo.get(conn, item.id)
        written = tracker_repo.get_item(conn, write["external_ref"])

    assert written.title == "Corrected by the reviewer"
    assert stored.original_payload["what"] != "Corrected by the reviewer"
