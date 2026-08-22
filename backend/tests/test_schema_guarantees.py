"""The gates that must hold in the database itself.

These are not tests of Python code. They open the database directly and try to
do the forbidden thing, which is exactly what the rubric's red flag describes:
"approval exists in the UI but is bypassable via the API". If a rule only holds
in the service layer, these tests fail.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

NOW = datetime.now(timezone.utc).isoformat()


def _source(conn: sqlite3.Connection, source_id: str, consent: int, status: str = "ingested") -> None:
    conn.execute(
        "INSERT INTO sources (id, title, source_type, meeting_date, participants, consent_flag,"
        " origin_format, file_path, content_hash, ingested_at, status, refusal_reason)"
        " VALUES (?, ?, 'transcript', '2024-11-18', '[]', ?, 'txt', 'f.txt', 'h', ?, ?, ?)",
        (source_id, source_id, consent, NOW, status, None if status != "refused" else "consent_flag is false"),
    )


def _extraction(conn: sqlite3.Connection, extraction_id: str, source_id: str, dedup: str = "d1") -> None:
    conn.execute(
        "INSERT INTO extractions (id, source_id, extraction_type, payload, original_payload,"
        " search_text, verbatim_quote, quote_verified, dedup_key, created_at)"
        " VALUES (?, ?, 'action', '{\"what\":\"x\"}', '{\"what\":\"x\"}', 'x', 'quote', 1, ?, ?)",
        (extraction_id, source_id, dedup, NOW),
    )


def _approve(conn: sqlite3.Connection, extraction_id: str) -> None:
    conn.execute(
        "UPDATE extractions SET status='approved', reviewer='esandu', reviewed_at=? WHERE id=?",
        (NOW, extraction_id),
    )


# --- M2 consent gate ---------------------------------------------------------


def test_extraction_against_non_consented_source_is_refused(conn):
    _source(conn, "no-consent", consent=0, status="refused")
    with pytest.raises(sqlite3.IntegrityError, match="consent_gate"):
        _extraction(conn, "e1", "no-consent")


def test_extraction_against_consented_source_is_allowed(conn):
    _source(conn, "yes-consent", consent=1)
    _extraction(conn, "e1", "yes-consent")
    assert conn.execute("SELECT COUNT(*) FROM extractions").fetchone()[0] == 1


def test_extraction_cannot_be_repointed_at_a_non_consented_source(conn):
    _source(conn, "yes-consent", consent=1)
    _source(conn, "no-consent", consent=0, status="refused")
    _extraction(conn, "e1", "yes-consent")
    with pytest.raises(sqlite3.IntegrityError, match="consent_gate"):
        conn.execute("UPDATE extractions SET source_id='no-consent' WHERE id='e1'")


# --- M6 approval gate --------------------------------------------------------


@pytest.mark.parametrize("status", ["pending", "rejected", "expired"])
def test_tracker_write_is_refused_unless_extraction_is_approved(conn, status):
    _source(conn, "s", consent=1)
    _extraction(conn, "e1", "s")
    if status == "rejected":
        conn.execute("UPDATE extractions SET status='rejected', reviewer='esandu', reviewed_at=? WHERE id='e1'", (NOW,))
    elif status == "expired":
        conn.execute("UPDATE extractions SET status='expired' WHERE id='e1'")

    with pytest.raises(sqlite3.IntegrityError, match="approval_gate"):
        conn.execute(
            "INSERT INTO tracker_writes (id, extraction_id, external_ref, provider, write_payload, written_at)"
            " VALUES ('t1', 'e1', 'MOCK-1', 'mock', '{}', ?)",
            (NOW,),
        )


def test_tracker_write_succeeds_once_approved(conn):
    _source(conn, "s", consent=1)
    _extraction(conn, "e1", "s")
    _approve(conn, "e1")
    conn.execute(
        "INSERT INTO tracker_writes (id, extraction_id, external_ref, provider, write_payload, written_at)"
        " VALUES ('t1', 'e1', 'MOCK-1', 'mock', '{}', ?)",
        (NOW,),
    )
    assert conn.execute("SELECT COUNT(*) FROM tracker_writes").fetchone()[0] == 1


def test_approving_without_a_reviewer_is_refused(conn):
    _source(conn, "s", consent=1)
    _extraction(conn, "e1", "s")
    with pytest.raises(sqlite3.IntegrityError, match="review_audit"):
        conn.execute("UPDATE extractions SET status='approved' WHERE id='e1'")


# --- M7 idempotency ----------------------------------------------------------


def test_reapproval_cannot_create_a_second_tracker_item(conn):
    _source(conn, "s", consent=1)
    _extraction(conn, "e1", "s")
    _approve(conn, "e1")
    conn.execute(
        "INSERT INTO tracker_writes (id, extraction_id, external_ref, provider, write_payload, written_at)"
        " VALUES ('t1', 'e1', 'MOCK-1', 'mock', '{}', ?)",
        (NOW,),
    )
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
        conn.execute(
            "INSERT INTO tracker_writes (id, extraction_id, external_ref, provider, write_payload, written_at)"
            " VALUES ('t2', 'e1', 'MOCK-2', 'mock', '{}', ?)",
            (NOW,),
        )


# --- Review state integrity --------------------------------------------------


@pytest.mark.parametrize("terminal", ["approved", "rejected", "expired"])
def test_terminal_status_cannot_be_reopened(conn, terminal):
    _source(conn, "s", consent=1)
    _extraction(conn, "e1", "s")
    if terminal == "expired":
        conn.execute("UPDATE extractions SET status='expired' WHERE id='e1'")
    else:
        conn.execute(
            "UPDATE extractions SET status=?, reviewer='esandu', reviewed_at=? WHERE id='e1'",
            (terminal, NOW),
        )
    with pytest.raises(sqlite3.IntegrityError, match="review_state"):
        conn.execute("UPDATE extractions SET status='pending' WHERE id='e1'")


def test_original_model_output_is_immutable_but_payload_is_editable(conn):
    _source(conn, "s", consent=1)
    _extraction(conn, "e1", "s")
    conn.execute("UPDATE extractions SET payload='{\"what\":\"edited by a human\"}' WHERE id='e1'")

    with pytest.raises(sqlite3.IntegrityError, match="original_payload is immutable"):
        conn.execute("UPDATE extractions SET original_payload='{\"what\":\"tampered\"}' WHERE id='e1'")


# --- Audit trail -------------------------------------------------------------


def test_review_events_are_append_only(conn):
    _source(conn, "s", consent=1)
    _extraction(conn, "e1", "s")
    conn.execute(
        "INSERT INTO review_events (id, extraction_id, event_type, actor, status_before, status_after,"
        " payload_after, created_at) VALUES ('r1', 'e1', 'approved', 'esandu', 'pending', 'approved', '{}', ?)",
        (NOW,),
    )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE review_events SET actor='someone else' WHERE id='r1'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM review_events WHERE id='r1'")


def test_write_attempts_are_append_only(conn):
    conn.execute(
        "INSERT INTO tracker_write_attempts (id, extraction_id, outcome, provider, attempted_at)"
        " VALUES ('a1', 'e1', 'blocked', 'mock', ?)",
        (NOW,),
    )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM tracker_write_attempts WHERE id='a1'")


# --- M9 direct messages, excluded by construction ----------------------------


def test_a_direct_message_cannot_be_stored_at_all(conn):
    _source(conn, "s", consent=1)
    with pytest.raises(sqlite3.IntegrityError, match="is_direct_message"):
        conn.execute(
            "INSERT INTO chat_messages (id, external_id, source_id, channel, author, ts, text,"
            " is_direct_message)"
            " VALUES ('s::m1', 'm1', 's', 'dm-thread', 'someone', ?, 'private', 1)",
            (NOW,),
        )


def test_noise_is_not_a_storable_classification(conn):
    _source(conn, "s", consent=1)
    with pytest.raises(sqlite3.IntegrityError, match="classification"):
        conn.execute(
            "INSERT INTO chat_messages (id, external_id, source_id, channel, author, ts, text,"
            " classification)"
            " VALUES ('s::m1', 'm1', 's', 'proj', 'someone', ?, 'lol', 'noise')",
            (NOW,),
        )


# --- Ingestion honesty -------------------------------------------------------


def test_a_refusal_must_state_its_reason(conn):
    with pytest.raises(sqlite3.IntegrityError, match="requires a stated reason"):
        conn.execute(
            "INSERT INTO sources (id, title, source_type, participants, consent_flag, ingested_at, status)"
            " VALUES ('s', 'T', 'transcript', '[]', 0, ?, 'refused')",
            (NOW,),
        )


# --- FTS5 --------------------------------------------------------------------


def test_fts_index_stays_in_step_with_segments_and_stems_words(conn):
    _source(conn, "s", consent=1)
    conn.execute(
        "INSERT INTO segments (id, source_id, segment_index, speaker, start_ts, text, char_start, char_end)"
        " VALUES ('seg1', 's', 0, 'Sarah Chen', '00:05:24', 'we should defer the reporting module', 0, 36)"
    )
    hits = conn.execute("SELECT rowid FROM segments_fts WHERE segments_fts MATCH 'deferred'").fetchall()
    assert len(hits) == 1, "porter stemming should match 'deferred' against 'defer'"

    conn.execute("DELETE FROM segments WHERE id='seg1'")
    assert conn.execute("SELECT COUNT(*) FROM segments_fts WHERE segments_fts MATCH 'defer'").fetchone()[0] == 0
