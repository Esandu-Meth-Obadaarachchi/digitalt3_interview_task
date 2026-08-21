"""M11 - the versioned outcome record.

    "The schema is documented in the repo with a version field. A second
     process can read a record and reconstruct the approved items without any
     access to the transcript store."

The second sentence is the whole capability, so it is tested literally: a
json.load of the file, no database connection, no application code beyond the
standard library.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.adapters.factory import get_store
from app.config import REPO_ROOT
from app.db import database
from app.errors import ConsentRefused, NotFoundError
from app.extraction.actions import extract_actions
from app.ingestion.service import ingest_from_manifest
from app.models.outcome import SCHEMA_VERSION
from app.outcome.record import build_record, emit, list_records, load_record, record_key
from app.review import queue

SPRINT = "meeting-sprint-planning-2024-11-18"
NO_CONSENT = "meeting-team-sync-2024-11-15"


@pytest.fixture()
def approved(settings, scripted_model):
    ingest_from_manifest(settings)
    scripted_model()
    extract_actions(SPRINT, settings)

    items = queue.list_queue(settings, source_id=SPRINT)
    for item in items[:3]:
        queue.approve(item.id, "esandu", "checked", settings=settings, write_through=False)
    queue.reject(items[3].id, "priya", "duplicate", settings)
    return settings


# --- the capability, tested literally -----------------------------------------


def test_a_second_process_can_reconstruct_the_items_with_no_database(approved):
    """json.load and nothing else. No database connection, no application code
    beyond the standard library."""
    record = emit(SPRINT, approved)
    path = Path(approved.document_store_dir) / record_key(SPRINT, record.record_version)

    raw = json.loads(path.read_text(encoding="utf-8"))

    assert raw["schema_version"] == SCHEMA_VERSION
    assert raw["consent_flag"] is True
    assert raw["actions"], "there should be approved actions to reconstruct"

    for item in raw["actions"]:
        # Everything needed to act on this item, and to check it, is here.
        assert item["payload"]["what"]
        assert item["approved_by"]
        assert item["citation"]["quote"]
        assert item["citation"]["source_id"]
        assert "quote_verified" in item["citation"]


def test_the_published_schema_exists_and_carries_a_version():
    schema = json.loads((REPO_ROOT / "docs" / "outcome_schema.json").read_text(encoding="utf-8"))

    assert schema["x-schema-version"] == SCHEMA_VERSION
    assert schema["x-consumer-contract"], "a schema alone cannot say what the fields mean"
    assert "consent_flag" in schema["required"]


def test_the_published_schema_matches_the_contract():
    """Generated, not maintained by hand. A hand-written schema drifts from
    what is actually emitted, and a consumer trusting the drifted version is
    worse off than one with no schema."""
    from app.models.outcome import OutcomeRecord

    published = json.loads((REPO_ROOT / "docs" / "outcome_schema.json").read_text(encoding="utf-8"))
    current = OutcomeRecord.model_json_schema()

    assert published["properties"].keys() == current["properties"].keys()
    assert published["required"] == current["required"]


# --- approved items only ------------------------------------------------------


def test_only_approved_items_are_included(approved):
    record = build_record(SPRINT, approved)

    with database.connect(approved) as conn:
        from app.db.repositories import extractions as extraction_repo
        from app.models.common import ReviewStatus

        allowed = {
            e.id for e in extraction_repo.list_extractions(
                conn, source_id=SPRINT, status=ReviewStatus.APPROVED
            )
        }

    assert record.total_items == len(allowed)
    for item in record.all_items:
        assert item.id in allowed


def test_what_was_excluded_is_counted_not_silently_dropped(approved):
    """Without these, an empty record is ambiguous between "nothing was found"
    and "nothing has been reviewed yet", and those call for opposite
    responses."""
    record = build_record(SPRINT, approved)

    assert record.pending_not_included > 0
    assert record.rejected_not_included == 1


def test_consent_is_carried_forward(approved):
    """A consumer has no other way to know whether the meeting these items came
    from permitted processing."""
    assert build_record(SPRINT, approved).consent_flag is True


def test_a_non_consented_source_gets_no_record(approved):
    """An empty record would imply the source was handled. Refusing says it
    was not."""
    with pytest.raises(ConsentRefused):
        build_record(NO_CONSENT, approved)


def test_an_unknown_source_is_a_clean_error(approved):
    with pytest.raises(NotFoundError):
        build_record("no-such-source", approved)


# --- versioning ---------------------------------------------------------------


def test_emitting_twice_produces_two_versions_and_leaves_the_first_alone(approved):
    """A consumer that read version 1 and acted on it should be able to see
    what it read."""
    first = emit(SPRINT, approved)
    original = load_record(SPRINT, first.record_version, approved)

    queue.approve(
        queue.list_queue(approved, source_id=SPRINT)[0].id,
        "esandu", settings=approved, write_through=False,
    )
    second = emit(SPRINT, approved)

    assert first.record_version == 1
    assert second.record_version == 2
    assert second.total_items > first.total_items

    unchanged = load_record(SPRINT, 1, approved)
    assert unchanged.record_id == original.record_id
    assert unchanged.total_items == first.total_items


def test_loading_without_a_version_gives_the_latest(approved):
    emit(SPRINT, approved)
    emit(SPRINT, approved)

    assert load_record(SPRINT, None, approved).record_version == 2


def test_records_are_listed_with_their_counts(approved):
    emit(SPRINT, approved)
    rows = list_records(approved, SPRINT)

    assert len(rows) == 1
    assert rows[0]["schema_version"] == SCHEMA_VERSION
    assert rows[0]["actions"] >= 1


def test_a_record_is_read_back_through_the_store_not_the_database(approved):
    """The claim is that a consumer needs no database. Reading it back any
    other way would not test that."""
    emit(SPRINT, approved)
    key = record_key(SPRINT, 1)

    assert get_store(approved).exists(key)
    get_store(approved).write(key, json.dumps({"schema_version": "9.9", "record_version": 1,
                                               "record_id": "x", "source_id": SPRINT,
                                               "source_title": "t", "source_type": "transcript",
                                               "consent_flag": True, "generated_at": "now"}))

    assert load_record(SPRINT, 1, approved).schema_version == "9.9", (
        "load_record must read the document, not the database copy"
    )


def test_a_human_edit_is_marked_on_the_item(approved):
    """A consumer acting on an item should be able to tell that a person
    changed it from what the model proposed."""
    item = queue.list_queue(approved, source_id=SPRINT)[0]
    queue.edit(item.id, dict(item.payload) | {"what": "Corrected"}, "esandu", settings=approved)
    queue.approve(item.id, "esandu", settings=approved, write_through=False)

    record = build_record(SPRINT, approved)
    edited = next(i for i in record.actions if i.id == item.id)

    assert edited.edited_by_reviewer is True
    assert edited.payload["what"] == "Corrected"
