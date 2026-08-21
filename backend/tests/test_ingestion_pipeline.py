"""M1 - validation grading and the end-to-end ingestion pipeline.

The capability test: ingest all three supplied meetings plus the deliberately
malformed file; the malformed file is rejected with a clear reason and does not
corrupt the store.
"""

from __future__ import annotations

import json

import pytest

from app.config import REPO_ROOT
from app.db import database
from app.db.repositories import segments as segment_repo
from app.db.repositories import sources as source_repo
from app.ingestion.normaliser import build_source_text, normalise, normalise_text
from app.ingestion.parsers import parse_transcript
from app.ingestion.reader import read_source_text
from app.ingestion.service import ingest_from_manifest, ingest_transcript
from app.ingestion.validator import summarise, validate
from app.models.common import SourceStatus
from app.models.ingestion import DefectCode, DefectSeverity
from app.models.source import SourceMetadata

TRANSCRIPTS = REPO_ROOT / "sample_data" / "transcripts"
GOLDEN = REPO_ROOT / "sample_data" / "golden"


def _validated(name: str, participants: list[str] | None = None):
    path = TRANSCRIPTS / name
    read = read_source_text(path)
    parsed = parse_transcript(path, read.text, read.encoding, read.bytes_read, participants)
    return validate(parsed.model_copy(update={"defects": read.defects + parsed.defects}))


# --- defect grading ----------------------------------------------------------


@pytest.mark.parametrize("name", ["sprint_planning.txt", "client_status_call.txt"])
def test_a_valid_transcript_produces_no_blocking_defect(name):
    assert _validated(name).ok is True


def test_the_malformed_sample_is_rejected_for_truncation():
    result = _validated("malformed_meeting.txt", ["Rachel Kim", "Alex Torres"])

    assert result.ok is False
    blocking = result.blocking_defects
    assert len(blocking) == 1
    assert blocking[0].code is DefectCode.TRUNCATED_MID_SENTENCE
    assert "upd" in (blocking[0].excerpt or "")
    assert "cut off" in blocking[0].detail


def test_missing_speaker_labels_warn_rather_than_block():
    """A transcript with an unlabelled line is realistic input. Rejecting it
    whole would be heavy-handed, so the defect travels with the source."""
    result = _validated("malformed_meeting.txt", ["Rachel Kim", "Alex Torres"])
    speaker_defects = [d for d in result.defects if d.code is DefectCode.MISSING_SPEAKER_LABEL]

    assert len(speaker_defects) == 3
    assert all(d.severity is DefectSeverity.WARNING for d in speaker_defects)


def test_truncation_detection_can_be_switched_off():
    """Documented as a heuristic, so it is overridable for a recording genuinely
    known to end mid-thought."""
    path = TRANSCRIPTS / "malformed_meeting.txt"
    read = read_source_text(path)
    parsed = parse_transcript(path, read.text, read.encoding, read.bytes_read, None)

    assert validate(parsed, check_truncation=False).ok is True
    assert validate(parsed, check_truncation=True).ok is False


def test_timestamps_going_backwards_produce_a_warning(tmp_path):
    path = tmp_path / "backwards.txt"
    path.write_text(
        "[00:00:10] Sarah Chen: First thing.\n[00:00:05] David Park: Out of order.\n",
        encoding="utf-8",
    )
    read = read_source_text(path)
    result = validate(parse_transcript(path, read.text, read.encoding, read.bytes_read, None))

    assert any(d.code is DefectCode.NON_MONOTONIC_TIMESTAMP for d in result.defects)
    assert result.ok is True


def test_summarise_names_the_reason_and_the_line():
    summary = summarise(_validated("malformed_meeting.txt"))
    assert summary.startswith("truncated_mid_sentence at line 17:")


# --- normalisation and citable offsets --------------------------------------


def test_character_offsets_resolve_against_the_source_text():
    result = _validated("sprint_planning.txt")
    segments, source_text = normalise("s", result.raw_segments)

    assert segments
    for segment in segments:
        assert source_text[segment.char_start : segment.char_end] == segment.text


def test_source_text_rebuilds_identically_from_stored_segments():
    """Offsets stored at ingestion must still resolve after a restart."""
    result = _validated("sprint_planning.txt")
    segments, source_text = normalise("s", result.raw_segments)
    assert build_source_text(segments) == source_text


def test_every_golden_quote_is_a_substring_of_the_normalised_source_text():
    """The definition of source text used for quote verification has to agree
    with the ground truth, or Phase 3 measures the wrong thing."""
    manifest = json.loads((REPO_ROOT / "sample_data" / "metadata" / "sources.json").read_text())
    by_id = {s["id"]: s for s in manifest["sources"]}

    texts = {}
    for source_id in ("meeting-sprint-planning-2024-11-18", "meeting-client-status-2024-08-19"):
        entry = by_id[source_id]
        path = REPO_ROOT / "sample_data" / entry["file_path"]
        read = read_source_text(path)
        parsed = parse_transcript(path, read.text, read.encoding, read.bytes_read, entry["participants"])
        _, texts[source_id] = normalise(source_id, parsed.raw_segments)

    checked = 0
    for filename, key in (
        ("golden_actions.json", "actions"),
        ("golden_decisions.json", "decisions"),
        ("golden_risks.json", "risks"),
    ):
        for item in json.loads((GOLDEN / filename).read_text())[key]:
            source_text = texts.get(item["source_id"])
            if source_text is None:
                continue
            assert normalise_text(item["verbatim_quote"]) in source_text, item["id"]
            checked += 1

    assert checked >= 15


# --- end to end --------------------------------------------------------------


def test_the_four_supplied_meetings_ingest_to_their_expected_outcomes(settings):
    outcomes = {o.source.id: o for o in ingest_from_manifest(settings)}

    assert outcomes["meeting-sprint-planning-2024-11-18"].source.status is SourceStatus.INGESTED
    assert outcomes["meeting-client-status-2024-08-19"].source.status is SourceStatus.INGESTED
    assert outcomes["meeting-team-sync-2024-11-15"].source.status is SourceStatus.REFUSED
    assert outcomes["meeting-design-review-2024-11-17"].source.status is SourceStatus.ERROR

    assert outcomes["meeting-sprint-planning-2024-11-18"].report.segments_parsed == 55
    assert outcomes["meeting-client-status-2024-08-19"].report.segments_parsed == 51


def test_a_rejected_source_leaves_no_segments_behind(settings):
    """"Does not corrupt the store" is the capability test's wording."""
    ingest_from_manifest(settings)

    with database.connect(settings) as conn:
        assert segment_repo.count_segments(conn, "meeting-design-review-2024-11-17") == 0
        assert segment_repo.count_segments(conn, "meeting-team-sync-2024-11-15") == 0
        assert conn.execute("SELECT COUNT(*) FROM segments").fetchone()[0] == 106

        stored = source_repo.get_source(conn, "meeting-design-review-2024-11-17")
        assert stored.status is SourceStatus.ERROR
        assert "truncated_mid_sentence" in stored.error_detail


def test_the_participant_who_never_speaks_is_named_in_the_report(settings):
    outcomes = {o.source.id: o for o in ingest_from_manifest(settings)}
    report = outcomes["meeting-sprint-planning-2024-11-18"].report

    assert report.silent_participants == ["Tom Reynolds"]
    assert "Tom Reynolds" not in report.speakers
    assert len(report.speakers) == 5


def test_reingestion_replaces_rather_than_duplicates(settings):
    manifest = json.loads((REPO_ROOT / "sample_data" / "metadata" / "sources.json").read_text())
    entry = next(s for s in manifest["sources"] if s["id"] == "meeting-sprint-planning-2024-11-18")

    for _ in range(3):
        ingest_transcript(SourceMetadata(**entry), settings=settings)

    with database.connect(settings) as conn:
        assert segment_repo.count_segments(conn, entry["id"]) == 55
        assert conn.execute("SELECT COUNT(*) FROM sources WHERE id = ?", (entry["id"],)).fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM ingestion_reports WHERE source_id = ?", (entry["id"],)).fetchone()[0] == 1


def test_the_keyword_index_stays_in_step_with_ingestion(settings):
    ingest_from_manifest(settings)

    with database.connect(settings) as conn:
        segments = conn.execute("SELECT COUNT(*) FROM segments").fetchone()[0]
        indexed = conn.execute("SELECT COUNT(*) FROM segments_fts").fetchone()[0]
        assert segments == indexed == 106

        hits = conn.execute(
            "SELECT s.speaker FROM segments_fts f JOIN segments s ON s.rowid = f.rowid"
            " WHERE segments_fts MATCH 'deferred'"
        ).fetchall()
        assert hits, "porter stemming should find 'defer' in the client status call"


# --- re-ingestion must not destroy a reviewer's work -------------------------


def test_reingesting_does_not_destroy_extractions(settings, scripted_model):
    """Found by pressing "Seed sample data" with 17 extractions in the queue.

    `INSERT OR REPLACE` on the sources row deleted it before re-inserting, and
    the delete fired ON DELETE CASCADE against extractions. Fourteen pending
    items vanished without a word.
    """
    from app.db.repositories import extractions as extraction_repo
    from app.extraction.actions import extract_actions

    ingest_from_manifest(settings)
    scripted_model()
    extract_actions("meeting-sprint-planning-2024-11-18", settings)

    with database.connect(settings) as conn:
        before = len(extraction_repo.list_extractions(conn, source_id="meeting-sprint-planning-2024-11-18"))
    assert before > 0

    ingest_from_manifest(settings)

    with database.connect(settings) as conn:
        after = len(extraction_repo.list_extractions(conn, source_id="meeting-sprint-planning-2024-11-18"))
    assert after == before


def test_reingesting_does_not_destroy_approved_work_or_its_audit(settings, scripted_model):
    from app.db.repositories import extractions as extraction_repo
    from app.extraction.actions import extract_actions
    from app.models.common import ReviewStatus
    from app.review import queue

    ingest_from_manifest(settings)
    scripted_model()
    extract_actions("meeting-sprint-planning-2024-11-18", settings)

    item = queue.list_queue(settings, source_id="meeting-sprint-planning-2024-11-18")[0]
    queue.approve(item.id, "esandu", "checked", settings=settings, write_through=False)

    ingest_from_manifest(settings)

    with database.connect(settings) as conn:
        stored = extraction_repo.get(conn, item.id)
        events = conn.execute("SELECT COUNT(*) AS n FROM review_events WHERE extraction_id = ?",
                              (item.id,)).fetchone()["n"]

    assert stored is not None
    assert stored.status is ReviewStatus.APPROVED
    assert stored.reviewer == "esandu"
    assert events == 1


def test_reingesting_identical_content_is_a_no_op(settings):
    """Nothing is rewritten, so citations into the existing segments survive."""
    ingest_from_manifest(settings)

    with database.connect(settings) as conn:
        first = segment_repo.list_segments(conn, "meeting-sprint-planning-2024-11-18")
        ingested_at = source_repo.get_source(conn, "meeting-sprint-planning-2024-11-18").ingested_at

    outcomes = {o.source.id: o for o in ingest_from_manifest(settings)}
    report = outcomes["meeting-sprint-planning-2024-11-18"].report

    assert report.unchanged is True
    with database.connect(settings) as conn:
        second = segment_repo.list_segments(conn, "meeting-sprint-planning-2024-11-18")
        assert source_repo.get_source(conn, "meeting-sprint-planning-2024-11-18").ingested_at == ingested_at
    assert [s.id for s in second] == [s.id for s in first]


def test_changed_content_is_reingested_rather_than_skipped(settings, tmp_path):
    """The no-op is keyed on the content hash, not on the id, so an edited
    transcript is genuinely re-read."""
    from app.models.source import SourceMetadata

    path = tmp_path / "changing.txt"
    path.write_text(
        "[00:00:01] Sarah Chen: We ship on Friday.\n[00:00:06] David Park: Understood.\n",
        encoding="utf-8",
    )
    meta = SourceMetadata(id="changing", title="Changing", source_type="transcript",
                          consent_flag=True, meeting_date="2024-11-18")

    first = ingest_transcript(meta, path, settings=settings)
    assert first.report.unchanged is False
    assert first.report.segments_parsed == 2

    path.write_text(
        "[00:00:01] Sarah Chen: We ship on Friday.\n[00:00:06] David Park: Understood.\n"
        "[00:00:12] Sarah Chen: One more thing, the audit is next week.\n",
        encoding="utf-8",
    )
    second = ingest_transcript(meta, path, settings=settings)

    assert second.report.unchanged is False
    assert second.report.segments_parsed == 3
    assert second.report.content_hash != first.report.content_hash


def test_a_refused_source_is_not_short_circuited(settings, sample_data_dir):
    """The no-op only applies to a source already stored as ingested. A refusal
    must be re-evaluated every time, in case consent changed."""
    import json

    manifest = json.loads((sample_data_dir / "metadata" / "sources.json").read_text())
    entry = next(s for s in manifest["sources"] if s["id"] == "meeting-team-sync-2024-11-15")

    for _ in range(2):
        outcome = ingest_transcript(SourceMetadata(**entry), settings=settings)
        assert outcome.source.status is SourceStatus.REFUSED
        assert outcome.report.bytes_read == 0
        assert outcome.report.unchanged is False
