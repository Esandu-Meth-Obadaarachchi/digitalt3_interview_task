"""M1 - parsing txt, vtt and json into the same normalised shape."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import REPO_ROOT
from app.ingestion.parsers import detect_format, parse_transcript
from app.ingestion.parsers.speakers import build_speaker_lookup, split_speaker
from app.ingestion.reader import read_source_text
from app.models.ingestion import DefectCode, TranscriptFormat

FIXTURES = REPO_ROOT / "sample_data" / "format_fixtures"
TRANSCRIPTS = REPO_ROOT / "sample_data" / "transcripts"
PARTICIPANTS = ["Lisa Tran", "David Park", "Sarah Chen", "Priya Sharma"]


def _parse(path: Path, participants: list[str] | None = PARTICIPANTS):
    read = read_source_text(path)
    return parse_transcript(path, read.text, read.encoding, read.bytes_read, participants)


# --- format detection --------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("client_status_call.txt", TranscriptFormat.TXT),
        ("client_status_call.vtt", TranscriptFormat.VTT),
        ("client_status_call.json", TranscriptFormat.JSON),
    ],
)
def test_format_is_detected(name, expected):
    path = (TRANSCRIPTS / name) if name.endswith(".txt") else (FIXTURES / name)
    assert detect_format(path, read_source_text(path).text) is expected


def test_a_transcript_opening_on_a_timestamp_is_not_mistaken_for_json():
    """Regression. "[00:00:05] ..." was sniffed as the start of a JSON array,
    which routed every plain transcript to the JSON parser."""
    path = TRANSCRIPTS / "sprint_planning.txt"
    assert detect_format(path, read_source_text(path).text) is TranscriptFormat.TXT


def test_content_overrides_a_contradicting_extension(tmp_path):
    """A VTT file renamed .txt is still VTT. Parsing it as prose loses every cue."""
    renamed = tmp_path / "actually_vtt.txt"
    renamed.write_text((FIXTURES / "client_status_call.vtt").read_text(encoding="utf-8"), encoding="utf-8")
    assert detect_format(renamed, read_source_text(renamed).text) is TranscriptFormat.VTT


def test_an_unrecognised_file_is_rejected_rather_than_guessed():
    result = _parse(FIXTURES / "unknown_format.dat")
    assert not result.ok
    assert any(d.code is DefectCode.UNKNOWN_FORMAT for d in result.blocking_defects)


# --- the point of building three parsers ------------------------------------


def test_all_three_formats_normalise_to_identical_segments():
    """One conversation, three encodings of it, one internal shape.

    This is what makes the format-agnostic claim checkable instead of asserted.
    """
    txt = _parse(TRANSCRIPTS / "client_status_call.txt")
    vtt = _parse(FIXTURES / "client_status_call.vtt")
    jsn = _parse(FIXTURES / "client_status_call.json")

    assert len(txt.raw_segments) == len(vtt.raw_segments) == len(jsn.raw_segments) == 51

    for a, b, c in zip(txt.raw_segments, vtt.raw_segments, jsn.raw_segments, strict=True):
        assert a.text == b.text == c.text
        assert a.speaker == b.speaker == c.speaker
        assert a.start_ts == b.start_ts == c.start_ts


# --- speaker attribution -----------------------------------------------------


def test_a_known_participant_is_matched_from_metadata():
    lookup = build_speaker_lookup(PARTICIPANTS)
    assert split_speaker("Sarah Chen: hello there", lookup) == ("Sarah Chen", "hello there")


def test_a_first_name_shared_by_two_participants_is_not_resolved():
    """Priya Sharma and Priya Menon are both in the sprint planning meeting.
    A bare "Priya" is ambiguous, and picking one would be a guess."""
    lookup = build_speaker_lookup(["Priya Sharma", "Priya Menon", "James Liu"])
    assert "priya" not in lookup
    assert "james" in lookup


def test_a_sentence_prefix_is_not_treated_as_a_speaker():
    """Splitting on the first colon would invent a speaker called Note."""
    speaker, text = split_speaker("Note: the deadline moved", build_speaker_lookup(PARTICIPANTS))
    assert speaker is None
    assert text == "Note: the deadline moved"


def test_an_unlabelled_line_stays_unattributed_and_says_so():
    result = _parse(TRANSCRIPTS / "malformed_meeting.txt", ["Rachel Kim", "Alex Torres"])

    unlabelled = [s for s in result.raw_segments if s.speaker is None]
    assert len(unlabelled) == 3

    warnings = [d for d in result.defects if d.code is DefectCode.MISSING_SPEAKER_LABEL]
    assert len(warnings) == 3
    assert all(not d.blocking for d in warnings)
    assert all("invent a speaker" in d.detail for d in warnings)


# --- read defects ------------------------------------------------------------


def test_invalid_utf8_is_a_blocking_defect_naming_the_byte():
    read = read_source_text(FIXTURES / "bad_encoding.txt")
    blocking = [d for d in read.defects if d.blocking]

    assert len(blocking) == 1
    assert blocking[0].code is DefectCode.UNDECODABLE_BYTES
    assert "0xa9" in blocking[0].detail
    assert blocking[0].line_number == 1


def test_an_empty_file_is_rejected():
    read = read_source_text(FIXTURES / "empty.txt")
    assert read.bytes_read == 0
    assert any(d.code is DefectCode.EMPTY_FILE and d.blocking for d in read.defects)


def test_a_missing_file_is_reported_not_raised():
    read = read_source_text(FIXTURES / "does_not_exist.txt")
    assert read.bytes_read == 0
    assert any(d.blocking for d in read.defects)


# --- vtt specifics -----------------------------------------------------------


def test_vtt_without_the_signature_is_rejected(tmp_path):
    bad = tmp_path / "no_signature.vtt"
    bad.write_text("00:00:01.000 --> 00:00:02.000\nhello\n", encoding="utf-8")
    result = _parse(bad)
    assert not result.ok
    assert any(d.code is DefectCode.MALFORMED_STRUCTURE for d in result.blocking_defects)


def test_vtt_speaker_prefix_convention_is_supported(tmp_path):
    """Not every VTT uses a voice tag. "Speaker: text" is just as common."""
    path = tmp_path / "prefix.vtt"
    path.write_text(
        "WEBVTT\n\n00:00:01.000 --> 00:00:04.000\nSarah Chen: We ship on Friday.\n\n"
        "00:00:04.000 --> 00:00:07.000\nDavid Park: Understood.\n",
        encoding="utf-8",
    )
    result = _parse(path)
    assert [s.speaker for s in result.raw_segments] == ["Sarah Chen", "David Park"]
    assert result.raw_segments[0].text == "We ship on Friday."


# --- json specifics ----------------------------------------------------------


def test_json_accepts_alternative_key_spellings(tmp_path):
    """Every speech-to-text tool names these keys differently. The accepted
    spellings are listed in one place so the rest of the system sees one shape."""
    path = tmp_path / "alt_keys.json"
    path.write_text(
        '{"utterances": ['
        '{"spk": "Sarah Chen", "begin": 12.5, "content": "We ship on Friday."},'
        '{"spk": "David Park", "begin": "00:00:20", "content": "Understood."}]}',
        encoding="utf-8",
    )
    result = _parse(path)
    assert [s.speaker for s in result.raw_segments] == ["Sarah Chen", "David Park"]
    assert result.raw_segments[0].start_ts == "00:00:12"
    assert result.raw_segments[1].start_ts == "00:00:20"


def test_invalid_json_is_rejected_with_a_line_number(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text('{"segments": [{"text": "hello"},]}', encoding="utf-8")
    result = _parse(path)
    assert not result.ok
    assert result.blocking_defects[0].line_number is not None
