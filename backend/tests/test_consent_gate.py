"""M2 - the consent gate.

The capability test: the supplied meeting with consent=false is never
transcribed, never sent to a model, and produces zero extracted items.
"""

from __future__ import annotations

import json

import pytest

from app.db import database
from app.db.repositories import segments as segment_repo
from app.db.repositories import sources as source_repo
from app.errors import ConsentRefused
from app.ingestion.consent import evaluate_consent, require_consent
from app.ingestion.service import ingest_transcript
from app.models.common import SourceStatus
from app.models.source import SourceMetadata


def _metadata(consent: bool, **overrides) -> SourceMetadata:
    payload = {
        "id": "meeting-under-test",
        "title": "Under test",
        "source_type": "transcript",
        "consent_flag": consent,
        "meeting_date": "2024-11-18",
        "participants": ["Karen Mitchell"],
        "file_path": "transcripts/team_sync_no_consent.txt",
    }
    payload.update(overrides)
    return SourceMetadata(**payload)


def test_consent_true_is_granted():
    assert evaluate_consent(_metadata(True)).granted is True


def test_consent_false_is_refused_with_a_stated_reason():
    decision = evaluate_consent(_metadata(False))
    assert decision.refused is True
    assert "consent_flag is not true" in decision.reason


def test_require_consent_raises_on_refusal():
    with pytest.raises(ConsentRefused):
        require_consent(_metadata(False))


def test_missing_consent_flag_fails_validation_rather_than_defaulting():
    """Absent consent is not consent, and a default either way would be a
    decision the source never made."""
    payload = {"id": "x", "title": "x", "source_type": "transcript"}
    with pytest.raises(Exception) as excinfo:
        SourceMetadata(**payload)
    assert "consent_flag" in str(excinfo.value)


def test_refused_source_is_never_opened(settings, sample_data_dir):
    """bytes_read = 0 is the machine-checkable evidence for the demo."""
    outcome = ingest_transcript(
        _metadata(False),
        sample_data_dir / "transcripts" / "team_sync_no_consent.txt",
        settings=settings,
    )

    assert outcome.ok is False
    assert outcome.report.bytes_read == 0
    assert outcome.report.content_hash is None
    assert outcome.source.status is SourceStatus.REFUSED
    assert outcome.segments == []


def test_refusal_is_stored_as_a_record_not_a_log_line(settings, sample_data_dir):
    ingest_transcript(
        _metadata(False),
        sample_data_dir / "transcripts" / "team_sync_no_consent.txt",
        settings=settings,
    )

    with database.connect(settings) as conn:
        stored = source_repo.get_source(conn, "meeting-under-test")
        report = source_repo.get_ingestion_report(conn, "meeting-under-test")
        assert segment_repo.count_segments(conn, "meeting-under-test") == 0

    assert stored is not None
    assert stored.status is SourceStatus.REFUSED
    assert stored.refusal_reason and "not true" in stored.refusal_reason
    assert report is not None and report.consent is not None
    assert report.consent.refused is True


def test_the_shipped_non_consented_meeting_is_refused(settings, sample_data_dir):
    """Against the real committed sample, not a fixture."""
    manifest = json.loads((sample_data_dir / "metadata" / "sources.json").read_text())
    entry = next(s for s in manifest["sources"] if s["id"] == "meeting-team-sync-2024-11-15")

    outcome = ingest_transcript(SourceMetadata(**entry), settings=settings)

    assert outcome.source.status is SourceStatus.REFUSED
    assert outcome.report.bytes_read == 0
    assert outcome.report.segments_parsed == 0


def test_no_extraction_can_exist_for_a_refused_source(settings, sample_data_dir):
    """The database backstop, independent of the service layer."""
    import sqlite3

    ingest_transcript(
        _metadata(False),
        sample_data_dir / "transcripts" / "team_sync_no_consent.txt",
        settings=settings,
    )

    with pytest.raises((ConsentRefused, sqlite3.IntegrityError)):
        with database.transaction(settings) as conn:
            conn.execute(
                "INSERT INTO extractions (id, source_id, extraction_type, payload, original_payload,"
                " search_text, verbatim_quote, created_at)"
                " VALUES ('x', 'meeting-under-test', 'action', '{}', '{}', 't', 'q', '2024-11-18')"
            )
