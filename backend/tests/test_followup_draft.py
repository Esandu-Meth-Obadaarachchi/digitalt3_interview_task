"""M12 - the recap draft, and the rule that the agent never sends.

    "Generate a recap email/message from approved items. Human edits and
     sends. Agent never sends."

The capability is one sentence and every clause of it is about who acts, so
that is what these tests are about. Three groups: what goes into a draft, what
happens to the two versions of the text, and who is allowed to send. The last
group is tested twice over, once through the service and once against raw
SQLite with no application code in the path, because a rule enforced only in
Python is a rule anybody with a database file can ignore.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from app.adapters.factory import get_notifier
from app.db import database
from app.db.repositories import extractions as extraction_repo
from app.errors import AgentSendRefused, NotFoundError, ReviewStateError
from app.extraction.actions import extract_actions
from app.followup import draft as service
from app.ingestion.service import ingest_from_manifest
from app.models.common import UNSPECIFIED, ReviewStatus
from app.review import queue

SPRINT = "meeting-sprint-planning-2024-11-18"
NOW = datetime(2026, 8, 22, 18, 0, tzinfo=timezone.utc)


@pytest.fixture()
def approved(settings, scripted_model):
    """Some approved items, some left pending, including unowned work."""
    ingest_from_manifest(settings)
    scripted_model()
    extract_actions(SPRINT, settings)

    items = queue.list_queue(settings, source_id=SPRINT)
    unowned = [i for i in items if str(i.payload.get("owner")) == UNSPECIFIED]
    assert unowned, "the sprint transcript plants commitments nobody owns"

    for item in {i.id: i for i in [*items[:4], *unowned]}.values():
        if item.id == items[-1].id:
            continue
        queue.approve(item.id, "esandu", settings=settings, write_through=False)
    return settings


# --- what goes into a draft ---------------------------------------------------


def test_a_draft_is_built_only_from_approved_items(approved):
    draft = service.build_draft(SPRINT, approved, now=NOW)

    with database.connect(approved) as conn:
        allowed = {
            e.id for e in extraction_repo.list_extractions(
                conn, source_id=SPRINT, status=ReviewStatus.APPROVED
            )
        }
        everything = {
            e.id for e in extraction_repo.list_extractions(conn, source_id=SPRINT)
        }

    assert everything - allowed, "the fixture must leave something unapproved"
    assert {line.extraction_id for line in draft.lines} <= allowed


def test_no_unapproved_quote_appears_in_the_text(approved):
    """The stronger version of the same rule: checked against the rendered
    message rather than against the structure behind it."""
    draft = service.build_draft(SPRINT, approved, now=NOW)

    with database.connect(approved) as conn:
        unapproved = [
            e for e in extraction_repo.list_extractions(conn, source_id=SPRINT)
            if e.status is not ReviewStatus.APPROVED
        ]

    for extraction in unapproved:
        assert extraction.verbatim_quote not in draft.generated_body


def test_every_line_carries_its_quote_into_the_message(approved):
    draft = service.build_draft(SPRINT, approved, now=NOW)

    assert draft.lines
    for line in draft.lines:
        assert line.citation.quote
        assert line.citation.quote in draft.generated_body


def test_a_commitment_nobody_owns_says_the_assignee_is_unspecified(approved):
    draft = service.build_draft(SPRINT, approved, now=NOW)

    unowned = [line for line in draft.lines if line.owner == UNSPECIFIED]
    assert unowned, "the fixture approved unowned work"
    assert "assignee UNSPECIFIED, nobody was named" in draft.generated_body
    for line in unowned:
        assert line.text in draft.generated_body, "the task is stated, not only the quote"


def test_a_commitment_with_no_date_says_so_rather_than_getting_one(approved):
    draft = service.build_draft(SPRINT, approved, now=NOW)
    if any(line.due_date == UNSPECIFIED for line in draft.lines):
        assert "no date stated" in draft.generated_body


def test_there_is_no_recap_of_nothing(settings):
    """An empty recap sent by mistake says the meeting produced nothing."""
    ingest_from_manifest(settings)
    with pytest.raises(NotFoundError, match="nothing approved"):
        service.build_draft(SPRINT, settings, now=NOW)


def test_an_unknown_source_is_not_found(settings):
    with pytest.raises(NotFoundError):
        service.build_draft("no-such-meeting", settings, now=NOW)


# --- the two versions of the text ---------------------------------------------


def test_each_draft_is_a_new_version(approved):
    first = service.create_draft(SPRINT, approved, now=NOW)
    second = service.create_draft(SPRINT, approved, now=NOW)

    assert first.draft_version == 1
    assert second.draft_version == 2
    assert len(service.list_drafts(SPRINT, approved)) == 2


def test_an_edit_is_stored_beside_the_generated_text_not_over_it(approved):
    created = service.create_draft(SPRINT, approved, now=NOW)

    edited = service.edit_draft(created.id, "Hi all, short version.", "esandu", approved)

    assert edited.generated_body == created.generated_body, "the machine text is untouched"
    assert edited.edited_body == "Hi all, short version."
    assert edited.body == "Hi all, short version.", "what would be sent is the human's version"
    assert edited.human_edited is True
    assert edited.status == "edited"
    assert edited.edited_by == "esandu"


def test_an_edit_records_who_made_it(approved):
    created = service.create_draft(SPRINT, approved, now=NOW)
    with pytest.raises(AgentSendRefused):
        service.edit_draft(created.id, "text", "  ", approved)


# --- who is allowed to send ---------------------------------------------------


def test_a_person_sends_it_and_the_send_is_recorded(approved):
    created = service.create_draft(SPRINT, approved, now=NOW)

    sent = service.send_draft(created.id, "esandu", "recap", approved, now=NOW)

    assert sent.status == "sent"
    assert sent.sent_by == "esandu"
    assert sent.notification_id
    posts = get_notifier(approved).list_posts("recap")
    assert len(posts) == 1
    assert posts[0].body == created.generated_body


def test_what_is_sent_is_what_the_person_edited(approved):
    created = service.create_draft(SPRINT, approved, now=NOW)
    service.edit_draft(created.id, "Short version, sent by hand.", "esandu", approved)

    service.send_draft(created.id, "esandu", "recap", approved, now=NOW)

    assert get_notifier(approved).list_posts("recap")[0].body == "Short version, sent by hand."


def test_a_send_with_nobody_behind_it_is_refused(approved):
    created = service.create_draft(SPRINT, approved, now=NOW)

    with pytest.raises(AgentSendRefused, match="never sends"):
        service.send_draft(created.id, "   ", "recap", approved)

    assert get_notifier(approved).list_posts() == [], "nothing was posted"


@pytest.mark.parametrize("name", ["agent", "Agent", "system", "scheduler", "bot", "llm", "model"])
def test_a_service_cannot_send_under_its_own_name(approved, name):
    created = service.create_draft(SPRINT, approved, now=NOW)

    with pytest.raises(AgentSendRefused, match="not a person"):
        service.send_draft(created.id, name, "recap", approved)


def test_a_draft_cannot_be_sent_twice(approved):
    created = service.create_draft(SPRINT, approved, now=NOW)
    service.send_draft(created.id, "esandu", "recap", approved, now=NOW)

    with pytest.raises(ReviewStateError):
        service.send_draft(created.id, "esandu", "recap", approved, now=NOW)


def test_a_sent_message_cannot_be_rewritten_afterwards(approved):
    created = service.create_draft(SPRINT, approved, now=NOW)
    service.send_draft(created.id, "esandu", "recap", approved, now=NOW)

    with pytest.raises(ReviewStateError):
        service.edit_draft(created.id, "something else entirely", "esandu", approved)


def test_nothing_in_the_scheduler_sends_a_follow_up():
    """Structural, because the capability is about what cannot happen.

    A scheduled job that sends a recap would satisfy every other test in this
    file and break the one rule M12 states.
    """
    import pathlib

    scheduler = pathlib.Path(__file__).resolve().parents[2] / "backend" / "app" / "scheduler"
    for path in scheduler.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "send_draft" not in text, f"{path.name} sends a follow-up"
        assert "followup" not in text, f"{path.name} reaches into follow-ups"


# --- the same rules, with no Python in the path -------------------------------


@pytest.fixture()
def stored(approved):
    """One draft in the database, and a raw connection to it."""
    created = service.create_draft(SPRINT, approved, now=NOW)
    return approved, created


def test_raw_sql_cannot_rewrite_the_generated_text(stored, conn):
    _, created = stored
    with pytest.raises(sqlite3.IntegrityError, match="generated_body is immutable"):
        conn.execute(
            "UPDATE followup_drafts SET generated_body = 'something else' WHERE id = ?",
            (created.id,),
        )


def test_raw_sql_cannot_send_without_a_person(stored, conn):
    _, created = stored
    with pytest.raises(sqlite3.IntegrityError, match="The agent never sends"):
        conn.execute("UPDATE followup_drafts SET status = 'sent' WHERE id = ?", (created.id,))


def test_raw_sql_cannot_send_as_a_service(stored, conn):
    _, created = stored
    with pytest.raises(sqlite3.IntegrityError, match="names a service"):
        conn.execute(
            "UPDATE followup_drafts SET status = 'sent', sent_by = 'scheduler' WHERE id = ?",
            (created.id,),
        )


def test_raw_sql_cannot_create_a_draft_already_sent(stored, conn):
    """Otherwise an INSERT walks straight past every rule written on UPDATE."""
    with pytest.raises(sqlite3.IntegrityError, match="created as a draft"):
        conn.execute(
            "INSERT INTO followup_drafts (id, source_id, draft_version, subject,"
            " generated_body, status, item_count, generated_at, sent_by)"
            " VALUES ('x', ?, 99, 's', 'b', 'sent', 1, '2026-08-22', 'agent')",
            (SPRINT,),
        )


def test_raw_sql_cannot_change_a_message_after_it_was_sent(stored, conn):
    settings, created = stored
    service.send_draft(created.id, "esandu", "recap", settings, now=NOW)

    with pytest.raises(sqlite3.IntegrityError, match="already sent"):
        conn.execute(
            "UPDATE followup_drafts SET edited_body = 'rewritten' WHERE id = ?", (created.id,)
        )


# --- the HTTP surface ---------------------------------------------------------


def test_the_api_previews_a_draft_with_its_lines(approved, client):
    body = client.get(f"/api/followups/preview/{SPRINT}").json()

    assert body["lines"], "the interface shows each line against its quote"
    assert body["generated_body"]


def test_the_api_refuses_a_send_with_no_person(approved, client):
    created = client.post(f"/api/followups/{SPRINT}").json()

    response = client.post(f"/api/followups/{created['id']}/send", json={"sent_by": ""})

    assert response.status_code == 403
    assert "never sends" in response.json()["detail"].lower()


def test_the_api_sends_as_a_named_person(approved, client):
    created = client.post(f"/api/followups/{SPRINT}").json()

    response = client.post(
        f"/api/followups/{created['id']}/send", json={"sent_by": "esandu", "channel": "recap"}
    )

    assert response.status_code == 200
    assert response.json()["sent_by"] == "esandu"


def test_the_api_has_no_way_to_send_without_naming_somebody(approved, client):
    """A default on sent_by would be the agent sending. There is none."""
    created = client.post(f"/api/followups/{SPRINT}").json()

    response = client.post(f"/api/followups/{created['id']}/send", json={})

    assert response.status_code == 422
