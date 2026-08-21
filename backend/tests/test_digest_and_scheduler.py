"""M10 - the scheduled digest, and the scheduler behind it.

    "The scheduler exists and is visible in the code. Running with a clock
     override produces a digest containing no unapproved items and no uncited
     lines."

Three properties, tested as properties: no unapproved item can appear, no line
can lack a citation, and the clock is injectable so none of this waits for six
o'clock.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.adapters.factory import get_notifier, get_store
from app.db import database
from app.db.repositories import extractions as extraction_repo
from app.extraction.actions import extract_actions
from app.ingestion.service import ingest_from_manifest
from app.models.common import ReviewStatus
from app.review import queue
from app.scheduler import jobs
from app.scheduler.digest import build_digest, emit_all, emit_digest, scopes

SPRINT = "meeting-sprint-planning-2024-11-18"
NOW = datetime(2026, 8, 21, 18, 0, tzinfo=timezone.utc)


@pytest.fixture()
def approved(settings, scripted_model):
    """Some approved items, and some deliberately left unapproved."""
    ingest_from_manifest(settings)
    scripted_model()
    extract_actions(SPRINT, settings)

    items = queue.list_queue(settings, source_id=SPRINT)
    for item in items[:4]:
        queue.approve(item.id, "esandu", settings=settings, write_through=False)
    queue.reject(items[4].id, "esandu", "duplicate", settings)
    return settings


# --- the three properties the capability test names ---------------------------


def test_a_digest_contains_no_unapproved_item(approved):
    digest = build_digest(SPRINT, approved, now=NOW)

    with database.connect(approved) as conn:
        allowed = {
            e.id for e in extraction_repo.list_extractions(
                conn, source_id=SPRINT, status=ReviewStatus.APPROVED
            )
        }
        everything = {e.id for e in extraction_repo.list_extractions(conn, source_id=SPRINT)}

    assert digest.lines, "the fixture approved items, so there should be lines"
    assert len(everything) > len(allowed), "the fixture must leave some items unapproved"
    for line in digest.lines:
        assert line.extraction_id in allowed


def test_every_line_carries_a_citation(approved):
    digest = build_digest(SPRINT, approved, now=NOW)

    for line in digest.lines:
        citation = line.citation
        assert citation.quote, "a line that cannot be cited is not written"
        assert citation.source_id
        assert line.because, "the reason a line is in its section travels with it"


def test_the_clock_override_produces_a_digest_for_any_date(approved):
    """The capability test's wording. Nothing here waits for six o'clock."""
    for moment in (NOW, NOW - timedelta(days=30), NOW + timedelta(days=90)):
        digest = build_digest(SPRINT, approved, now=moment, trigger="clock_override")
        assert digest.digest_date == moment.date().isoformat()
        assert digest.trigger == "clock_override"


# --- the 3/2/1 shape ----------------------------------------------------------


def test_the_sections_are_capped_at_three_two_one(approved):
    digest = build_digest(SPRINT, approved, now=NOW)

    assert len(digest.moved) <= 3
    assert len(digest.attention) <= 2
    assert len(digest.to_decide) <= 1


def test_no_item_appears_in_two_sections(approved):
    """Found by rendering one and reading it. A blocker approved today is both
    progress and a problem, and printed under both it fills two of six lines
    with one fact."""
    digest = build_digest(SPRINT, approved, now=NOW)
    ids = [line.extraction_id for line in digest.lines]

    assert len(ids) == len(set(ids))


def test_an_empty_scope_says_so_rather_than_rendering_blank(settings):
    ingest_from_manifest(settings)
    digest = build_digest(SPRINT, settings, now=NOW)

    assert digest.empty
    assert digest.considered == 0
    assert "Nothing approved in scope" in digest.render()


def test_the_rendered_digest_states_that_nothing_unapproved_is_in_it(approved):
    body = build_digest(SPRINT, approved, now=NOW).render()

    assert "Nothing unapproved appears in this digest" in body
    assert "## Moved" in body and "## Needs attention" in body and "## To decide" in body


# --- storage and posting ------------------------------------------------------


def test_emitting_writes_through_the_store_and_posts_through_the_notifier(approved):
    digest = emit_digest(SPRINT, approved, now=NOW, trigger="clock_override")

    document = get_store(approved).read(f"digests/{digest.digest_date}/{SPRINT}.md")
    assert document is not None
    assert "## Moved" in document

    posts = get_notifier(approved).list_posts(SPRINT)
    assert posts and posts[-1].subject.endswith(digest.digest_date)


def test_a_digest_is_recorded_so_it_can_be_listed(approved):
    emit_digest(SPRINT, approved, now=NOW)

    with database.connect(approved) as conn:
        rows = conn.execute("SELECT scope_key, digest_date, trigger FROM digests").fetchall()

    assert len(rows) == 1
    assert rows[0]["scope_key"] == SPRINT


def test_running_the_same_day_twice_replaces_rather_than_duplicates(approved):
    emit_digest(SPRINT, approved, now=NOW)
    emit_digest(SPRINT, approved, now=NOW)

    with database.connect(approved) as conn:
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM digests WHERE scope_key = ? AND digest_date = ?",
            (SPRINT, NOW.date().isoformat()),
        ).fetchone()["n"]

    assert count == 1, "a scheduled job that runs twice must not produce two digests for one day"


def test_every_scope_gets_a_digest(approved):
    digests = emit_all(approved, now=NOW, trigger="clock_override", post=False)
    assert {d.scope_key for d in digests} == {key for key, _ in scopes(approved)}


# --- the scheduler itself -----------------------------------------------------


def test_the_scheduler_registers_both_jobs_with_real_fire_times(settings, monkeypatch):
    """A next-run timestamp that advances on its own is the difference between
    a scheduler and a constant."""
    monkeypatch.setenv("SCHEDULER_ENABLED", "true")
    from app.config import get_settings

    get_settings.cache_clear()
    cfg = get_settings()

    jobs.start(cfg)
    try:
        status = jobs.status(cfg)
        assert status.running
        ids = {job.id for job in status.jobs}
        assert ids == {"end_of_day_digest", "expiry_sweep"}
        for job in status.jobs:
            assert job.next_run_at, "a job with no next run is not scheduled"
            assert datetime.fromisoformat(job.next_run_at) > datetime.now(timezone.utc)
    finally:
        jobs.shutdown()
        get_settings.cache_clear()


def test_status_is_honest_when_the_scheduler_is_disabled(settings):
    """An empty job list would read as a bug. Saying it is off does not."""
    status = jobs.status(settings)

    assert status.running is False
    assert status.enabled is False
    assert len(status.jobs) == 2, "the jobs that WOULD run are still described"
    assert all(job.next_run_at is None for job in status.jobs)


def test_the_expiry_job_is_the_safe_default_not_an_approval(approved):
    """Nothing is ever approved by the passage of time."""
    with database.connect(approved) as conn:
        before = extraction_repo.counts_by_status(conn, SPRINT)

    with database.transaction(approved) as conn:
        conn.execute("UPDATE extractions SET expires_at = ? WHERE status = 'pending'",
                     ("2000-01-01T00:00:00+00:00",))

    expired = jobs.run_expiry_job(approved)

    with database.connect(approved) as conn:
        after = extraction_repo.counts_by_status(conn, SPRINT)

    assert expired > 0
    assert after.get("pending", 0) == 0
    assert after.get("approved", 0) == before.get("approved", 0), "no item may become approved"
    assert after.get("expired", 0) == expired


def test_the_digest_job_runs_what_the_endpoint_runs(approved):
    """Same function, so what is demonstrated is what runs unattended."""
    written = jobs.run_digest_job(approved)

    with database.connect(approved) as conn:
        rows = conn.execute("SELECT DISTINCT trigger FROM digests").fetchall()

    assert written == len(scopes(approved))
    assert [r["trigger"] for r in rows] == ["scheduler"]
