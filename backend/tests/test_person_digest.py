"""M13 - the per-person digest.

    "Per-person view of their commitments. Person with no commitments gets no
     digest."

The rules tested here are the ones a reviewer would try to break: an
unapproved item must not reach a digest, a person with nothing must not get
one, unowned work must be visible without being assigned to anybody, and two
people sharing a first name must be grouped without losing which of them said
what.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.adapters.factory import get_store
from app.db import database
from app.db.repositories import extractions as extraction_repo
from app.extraction.actions import extract_actions
from app.ingestion.service import ingest_from_manifest
from app.models.common import UNSPECIFIED, ExtractionType, ReviewStatus
from app.people.identity import UNASSIGNED
from app.review import queue
from app.scheduler.person_digest import (
    build_person_digest,
    emit_all_people,
    emit_person_digest,
    people,
)

SPRINT = "meeting-sprint-planning-2024-11-18"
NOW = datetime(2026, 8, 22, 18, 0, tzinfo=timezone.utc)


@pytest.fixture()
def approved(settings, scripted_model):
    """Approved actions, with some deliberately left pending.

    Chosen rather than sliced: the unowned commitments are approved on purpose,
    because the unassigned digest is half of what M13 asks for, and the last
    item is left pending on purpose, because a digest containing an unapproved
    item is the failure these tests exist to catch.
    """
    ingest_from_manifest(settings)
    scripted_model()
    extract_actions(SPRINT, settings)

    items = queue.list_queue(settings, source_id=SPRINT)
    unowned = [i for i in items if str(i.payload.get("owner")) == UNSPECIFIED]
    assert unowned, "the sprint transcript plants commitments nobody owns"

    for item in {i.id: i for i in [*items[:5], *unowned]}.values():
        if item.id == items[-1].id:
            continue
        queue.approve(item.id, "esandu", settings=settings, write_through=False)
    return settings


def _owners(settings) -> list[str]:
    with database.connect(settings) as conn:
        return [
            str(e.payload.get("owner"))
            for e in extraction_repo.list_extractions(
                conn, extraction_type=ExtractionType.ACTION, status=ReviewStatus.APPROVED
            )
        ]


# --- the approval gate holds here too -----------------------------------------


def test_a_person_digest_contains_no_unapproved_item(approved):
    with database.connect(approved) as conn:
        allowed = {
            e.id for e in extraction_repo.list_extractions(
                conn, extraction_type=ExtractionType.ACTION, status=ReviewStatus.APPROVED
            )
        }
        everything = {
            e.id for e in extraction_repo.list_extractions(
                conn, extraction_type=ExtractionType.ACTION
            )
        }

    assert everything - allowed, "the fixture must leave something unapproved"

    seen = set()
    for person in people(approved):
        digest = build_person_digest(person.key, approved, now=NOW)
        seen |= {line.extraction_id for line in digest.commitments}

    assert seen, "the fixture approved items, so there should be lines"
    assert seen <= allowed


def test_every_line_carries_a_quote_from_its_source(approved):
    for person in people(approved):
        for line in build_person_digest(person.key, approved, now=NOW).commitments:
            assert line.citation.quote, "a line nobody can check is not a citation"
            assert line.citation.source_id


# --- the rule the specification states outright -------------------------------


def test_a_person_with_no_commitments_gets_no_digest(approved):
    digest = emit_person_digest("nobody-by-this-name", approved, now=NOW)
    assert digest is None


def test_nothing_is_written_for_a_person_with_no_commitments(approved):
    emit_person_digest("nobody-by-this-name", approved, now=NOW)

    written = [d.key for d in get_store(approved).list_documents("digests/")]
    assert not any("nobody-by-this-name" in key for key in written)

    with database.connect(approved) as conn:
        rows = conn.execute(
            "SELECT COUNT(*) AS n FROM digests WHERE scope_key = 'nobody-by-this-name'"
        ).fetchone()
    assert rows["n"] == 0


def test_emit_all_writes_one_digest_per_person_who_has_something(approved):
    written = emit_all_people(approved, now=NOW)

    assert written, "somebody was approved, so somebody has a digest"
    assert all(not d.empty for d in written), "an empty digest is never written"
    assert len({d.person_key for d in written}) == len(written), "one per person"


# --- unowned work is visible, and never assigned to anybody -------------------


def test_unowned_commitments_get_their_own_digest(approved):
    assert UNSPECIFIED in _owners(approved)

    digest = build_person_digest(UNASSIGNED, approved, now=NOW)

    assert digest.unassigned is True
    assert digest.commitments
    assert digest.display_name == "Assignee unspecified"


def test_the_unassigned_digest_states_the_task_and_says_the_assignee_is_unspecified(approved):
    assert UNSPECIFIED in _owners(approved)

    rendered = build_person_digest(UNASSIGNED, approved, now=NOW).render()

    assert "Assignee unspecified" in rendered
    assert "assignee UNSPECIFIED, nobody was named" in rendered
    for line in build_person_digest(UNASSIGNED, approved, now=NOW).commitments:
        assert line.text in rendered, "the task itself is stated, not only the quote"


def test_unowned_work_never_lands_in_a_named_person_digest(approved):
    for person in people(approved):
        if person.unassigned:
            continue
        digest = build_person_digest(person.key, approved, now=NOW)
        assert all(line.owner_as_stated != UNSPECIFIED for line in digest.commitments)


# --- grouping, and the evidence that survives it ------------------------------


def test_two_people_sharing_a_first_name_share_one_digest(settings, scripted_model):
    """Instructed behaviour, so it is pinned rather than assumed."""
    ingest_from_manifest(settings)
    scripted_model()
    extract_actions(SPRINT, settings)

    items = queue.list_queue(settings, source_id=SPRINT)
    for item in items:
        queue.approve(item.id, "esandu", settings=settings, write_through=False)

    keys = [p.key for p in people(settings)]
    assert len(keys) == len(set(keys)), "one key per person, never two buckets for one name"

    for person in people(settings):
        if not person.ambiguous:
            continue
        digest = build_person_digest(person.key, settings, now=NOW)
        rendered = digest.render()
        assert "Grouped by first name" in rendered
        for alias in person.aliases:
            assert alias in rendered, "the full names it covers are named"
        break


def test_every_line_names_the_owner_as_the_transcript_stated_it(approved):
    """What makes the grouping honest: the merge is visible line by line."""
    for person in people(approved):
        if person.unassigned:
            continue
        for line in build_person_digest(person.key, approved, now=NOW).commitments:
            assert line.owner_as_stated in person.aliases


# --- dates are reported, never invented ---------------------------------------


def test_a_commitment_with_no_date_says_so_rather_than_getting_one(approved):
    for person in people(approved):
        digest = build_person_digest(person.key, approved, now=NOW)
        undated = [line for line in digest.commitments if not line.dated]
        if not undated:
            continue
        rendered = digest.render()
        assert "due UNSPECIFIED, no date was stated" in rendered
        return


# --- the clock is injectable --------------------------------------------------


def test_the_clock_override_produces_a_digest_for_any_day(approved):
    for person in people(approved):
        digest = build_person_digest(person.key, approved, now=NOW)
        assert digest.digest_date == "2026-08-22"
        assert digest.generated_at.startswith("2026-08-22")
        return


def test_a_stored_digest_records_its_scope_type_as_person(approved):
    written = emit_all_people(approved, now=NOW)
    assert written

    with database.connect(approved) as conn:
        rows = conn.execute(
            "SELECT scope_type, scope_key FROM digests WHERE scope_type = 'person'"
        ).fetchall()

    assert {row["scope_key"] for row in rows} == {d.person_key for d in written}


def test_person_digests_are_not_posted_by_default(approved):
    """One person's workload posted into a shared channel is a different thing
    from the digest they asked for."""
    from app.adapters.factory import get_notifier

    emit_all_people(approved, now=NOW)
    assert get_notifier(approved).list_posts() == []


# --- the HTTP surface ---------------------------------------------------------


def test_the_api_lists_people_with_the_grouping_visible(approved, client):
    body = client.get("/api/digests/people").json()

    assert body, "somebody was approved"
    for person in body:
        assert set(person) == {"key", "display_name", "aliases", "ambiguous", "unassigned"}
    keys = [p["key"] for p in body]
    assert keys[-1] == UNASSIGNED, "unowned work sorts last"


def test_the_api_previews_a_person_without_writing_anything(approved, client):
    key = client.get("/api/digests/people").json()[0]["key"]

    body = client.get(f"/api/digests/people/{key}").json()

    assert body["commitments"]
    with database.connect(approved) as conn:
        stored = conn.execute("SELECT COUNT(*) AS n FROM digests").fetchone()
    assert stored["n"] == 0, "a preview writes nothing"


def test_the_api_refuses_to_produce_a_digest_for_somebody_with_nothing(approved, client):
    assert client.get("/api/digests/people/nobody/markdown").status_code == 404
    assert client.post("/api/digests/people/nobody").status_code == 404


def test_the_api_runs_the_whole_person_job(approved, client):
    body = client.post("/api/digests/people/run/all").json()

    assert body
    assert all(item["commitments"] for item in body), "nobody empty is written"


def test_the_scheduler_status_describes_the_person_digest(approved, client):
    body = client.get("/api/digests/schedule").json()
    digest_job = next(j for j in body["jobs"] if j["id"] == "end_of_day_digest")
    assert "person" in digest_job["description"]
