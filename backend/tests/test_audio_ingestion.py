"""M1 audio - a recording becomes segments, and says what it is.

Two things separate a machine transcript from a typed one, and both change what
a quote drawn from it means. There are no speaker labels, because whisper does
not diarise and guessing from context would be inventing an attribution. And
the words are a model's best guess: transcribing a test clip on this machine
turned "Nuwan" into "new one". Every test below is about one of those two
properties, or about the pipeline treating a recording as one more input rather
than a second pipeline.

The stub transcriber is used throughout. No test downloads a 140MB model or
depends on a machine having audio libraries, and the stub keeps the shape that
matters: no speakers, real timestamps, one segment per line.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.audio.transcribe import (
    AUDIO_EXTENSIONS,
    FakeTranscriber,
    FasterWhisperTranscriber,
    format_timestamp,
    get_transcriber,
    looks_like_audio,
)
from app.db import database
from app.db.repositories import segments as segment_repo
from app.ingestion.service import ingest_audio
from app.models.common import SourceStatus, SourceType
from app.models.source import SourceMetadata


def _recording(tmp_path: Path, heard: list[str] | None = None, name: str = "standup.wav") -> Path:
    """A file with an audio extension, plus what the stub should 'hear'."""
    path = tmp_path / name
    path.write_bytes(b"RIFF" + b"\0" * 64)
    if heard is not None:
        path.with_suffix(".txt").write_text("\n".join(heard), encoding="utf-8")
    return path


def _metadata(consent: bool = True, participants: list[str] | None = None) -> SourceMetadata:
    return SourceMetadata(
        id="meeting-recorded-2026-09-20",
        title="Recorded standup",
        source_type=SourceType.AUDIO,
        consent_flag=consent,
        meeting_date="2026-09-20",
        participants=participants or ["Ranidu", "Esandu"],
        file_path="standup.wav",
    )


# --- the gate runs first, exactly as it does for text -------------------------


def test_consent_is_checked_before_the_recording_is_opened(settings, tmp_path):
    """The evidence is the same as everywhere else: nothing was read."""
    path = _recording(tmp_path, ["I will finish the booking engine by Friday."])

    outcome = ingest_audio(_metadata(consent=False), path, settings)

    assert outcome.source.status is SourceStatus.REFUSED
    assert outcome.report.bytes_read == 0
    assert outcome.report.content_hash is None
    assert outcome.segments == []


def test_a_refused_recording_stores_no_segments(settings, tmp_path):
    path = _recording(tmp_path, ["Something private."])
    ingest_audio(_metadata(consent=False), path, settings)

    with database.connect(settings) as conn:
        assert segment_repo.list_segments(conn, "meeting-recorded-2026-09-20") == []


# --- transcription produces the same shape the text parsers produce -----------


def test_a_recording_becomes_segments_with_offsets(settings, tmp_path):
    heard = [
        "Right, quick standup.",
        "I will finish the booking engine by Friday.",
        "The staging server is down, so I am blocked on testing.",
    ]
    path = _recording(tmp_path, heard)

    outcome = ingest_audio(_metadata(), path, settings)

    assert outcome.source.status is SourceStatus.INGESTED
    assert outcome.source.origin_format == "audio"
    assert len(outcome.segments) == 3

    for segment in outcome.segments:
        assert segment.char_end > segment.char_start, "offsets index into the source text"
        assert segment.start_ts, "a citation has to point at a timestamp"


def test_the_stored_text_supports_quote_verification(settings, tmp_path):
    """The whole point of reusing the text pipeline: a quote from a recording
    is checked exactly as a quote from a typed transcript is."""
    from app.ingestion.normaliser import normalise_text

    heard = ["I will finish the booking engine by Friday."]
    path = _recording(tmp_path, heard)
    ingest_audio(_metadata(), path, settings)

    with database.connect(settings) as conn:
        text = segment_repo.get_source_text(conn, "meeting-recorded-2026-09-20")

    assert normalise_text(heard[0]) in normalise_text(text)


def test_timestamps_are_the_same_shape_as_the_txt_parser_produces():
    assert format_timestamp(0) == "00:00:00"
    assert format_timestamp(65) == "00:01:05"
    assert format_timestamp(3725) == "01:02:05"
    assert format_timestamp(-4) == "00:00:00", "a negative offset is not a time"


# --- nobody is attributed, and the report says so -----------------------------


def test_no_segment_is_attributed_to_a_speaker(settings, tmp_path):
    """Whisper does not diarise. Filling this in from the participant list or
    from the previous line would be inventing an attribution, which is the one
    thing the brief's first rule forbids."""
    path = _recording(tmp_path, ["Ranidu here.", "And Esandu."])

    outcome = ingest_audio(_metadata(), path, settings)

    assert all(segment.speaker is None for segment in outcome.segments)
    assert outcome.report.speakers == []


def test_every_participant_is_reported_as_silent(settings, tmp_path):
    """Not because they said nothing, but because nothing knows who spoke. The
    report says every named participant is unheard rather than claiming
    attendance it cannot support."""
    path = _recording(tmp_path, ["Someone said something."])

    outcome = ingest_audio(_metadata(participants=["Ranidu", "Esandu"]), path, settings)

    assert outcome.report.silent_participants == ["Ranidu", "Esandu"]


def test_the_report_warns_that_the_words_are_a_machine_transcription(settings, tmp_path):
    path = _recording(tmp_path, ["Nuwan, can you review the pull request?"])

    outcome = ingest_audio(_metadata(), path, settings)

    warnings = [d for d in outcome.report.defects if not d.blocking]
    assert warnings, "an audio source is never defect-free, it is machine-heard"
    assert any("best guess" in d.detail for d in warnings)
    assert any("no speaker labels" in d.detail for d in warnings)


def test_the_defect_names_the_model_that_produced_the_words(settings, tmp_path):
    path = _recording(tmp_path, ["Anything."])
    outcome = ingest_audio(_metadata(), path, settings)
    assert any("fake:stub" in d.detail for d in outcome.report.defects)


# --- refusals, each naming the real problem -----------------------------------


def test_a_file_that_is_not_audio_is_refused_before_any_decoding(settings, tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("[00:00:01] Ranidu: Hello.\n", encoding="utf-8")

    outcome = ingest_audio(_metadata(), path, settings)

    assert outcome.source.status is SourceStatus.ERROR
    assert "not an audio format" in outcome.source.error_detail


def test_a_missing_file_says_so(settings, tmp_path):
    outcome = ingest_audio(_metadata(), tmp_path / "gone.wav", settings)
    assert outcome.source.status is SourceStatus.ERROR
    assert "no file at" in outcome.source.error_detail


def test_transcription_switched_off_is_stated_rather_than_silent(settings, tmp_path, monkeypatch):
    path = _recording(tmp_path, ["Anything."])
    monkeypatch.setattr(settings, "whisper_enabled", False)

    outcome = ingest_audio(_metadata(), path, settings)

    assert outcome.source.status is SourceStatus.ERROR
    assert "switched off" in outcome.source.error_detail


def test_a_decoder_failure_is_a_source_error_not_a_crash(settings, tmp_path, monkeypatch):
    """A corrupt recording is a problem with the source, and the pipeline
    records it the same way it records an unparseable transcript."""
    path = _recording(tmp_path, ["Anything."])

    def explode(self, _path):
        raise RuntimeError("Invalid data found when processing input")

    monkeypatch.setattr(FakeTranscriber, "transcribe", explode)

    outcome = ingest_audio(_metadata(), path, settings)

    assert outcome.source.status is SourceStatus.ERROR
    assert "transcription failed" in outcome.source.error_detail
    assert outcome.report.bytes_read > 0, "the file was opened, unlike a consent refusal"


def test_silence_is_not_an_empty_success(settings, tmp_path, monkeypatch):
    path = _recording(tmp_path, ["Anything."])
    monkeypatch.setattr(
        FakeTranscriber,
        "transcribe",
        lambda self, _p: __import__(
            "app.audio.transcribe", fromlist=["Transcription"]
        ).Transcription(segments=[], model_name="stub", provider="fake"),
    )

    outcome = ingest_audio(_metadata(), path, settings)

    assert outcome.source.status is SourceStatus.ERROR
    assert "nothing audible" in outcome.source.error_detail


# --- the transcriber is swappable, the same way the model provider is ---------


def test_the_configured_transcriber_is_the_one_returned(settings):
    assert isinstance(get_transcriber(settings), FakeTranscriber)


def test_the_real_transcriber_reports_its_own_availability(settings):
    """It says whether it can run here rather than failing at the first file."""
    usable, detail = FasterWhisperTranscriber(settings).available()
    assert isinstance(usable, bool)
    assert detail, "available() must always explain itself"


def test_the_stub_is_marked_as_a_stub(settings):
    transcriber = FakeTranscriber()
    assert transcriber.describe() == "fake:stub"
    assert "measures nothing" in transcriber.transcribe(Path("nowhere.wav")).segments[0].text


@pytest.mark.parametrize("name", ["a.wav", "b.MP3", "c.m4a", "d.aiff", "e.flac", "f.webm"])
def test_common_recording_formats_are_recognised(name):
    assert looks_like_audio(name)


@pytest.mark.parametrize("name", ["a.txt", "b.json", "c.vtt", "d.pdf", "e"])
def test_a_document_is_not_a_recording(name):
    assert not looks_like_audio(name)


def test_the_extension_list_is_stated_rather_than_guessed():
    assert ".wav" in AUDIO_EXTENSIONS and ".txt" not in AUDIO_EXTENSIONS


# --- the worker boundary ------------------------------------------------------
# faiss and ctranslate2 each link their own OpenMP runtime, and loading both in
# one process aborts on macOS. The API loads faiss, so whisper runs in a
# subprocess. These tests cover the boundary without loading a model.


def test_the_worker_reports_bad_arguments_as_data(settings):
    """It prints JSON and exits non-zero rather than raising a traceback the
    caller would have to parse out of stderr."""
    import json
    import subprocess
    import sys
    from app.audio import transcribe as module

    worker = Path(module.__file__).with_name("whisper_worker.py")
    finished = subprocess.run([sys.executable, str(worker)], capture_output=True, text=True)

    assert finished.returncode == 2
    assert "error" in json.loads(finished.stdout)


def test_the_worker_script_sits_where_the_transcriber_looks_for_it(settings):
    from app.audio import transcribe as module

    assert Path(module.__file__).with_name("whisper_worker.py").exists()


def test_worker_output_becomes_segments_with_no_speaker(settings, monkeypatch):
    """The mapping from worker JSON to RawSegment, without a model."""
    import json
    import subprocess

    payload = {
        "segments": [
            {"start": 0.0, "end": 4.5, "text": "Right, quick standup."},
            {"start": 4.5, "end": 65.0, "text": "I will finish the booking engine by Friday."},
        ],
        "language": "en",
        "duration": 65.0,
        "model": "base",
    }

    class Finished:
        returncode = 0
        stdout = json.dumps(payload)
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: Finished())

    result = FasterWhisperTranscriber(settings).transcribe(Path("anything.wav"))

    assert [s.text for s in result.segments] == [c["text"] for c in payload["segments"]]
    assert all(s.speaker is None for s in result.segments)
    assert result.segments[1].start_ts == "00:00:04"
    assert result.language == "en" and result.duration_seconds == 65.0


def test_a_worker_error_becomes_a_readable_failure(settings, monkeypatch):
    import json
    import subprocess

    class Finished:
        returncode = 1
        stdout = json.dumps({"error": "RuntimeError: Invalid data found when processing input"})
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: Finished())

    with pytest.raises(RuntimeError, match="Invalid data"):
        FasterWhisperTranscriber(settings).transcribe(Path("broken.wav"))


def test_a_silent_worker_is_a_failure_rather_than_an_empty_transcript(settings, monkeypatch):
    """A worker killed by the OS prints nothing. Reading that as 'no speech'
    would turn a crash into an empty meeting."""
    import subprocess

    class Finished:
        returncode = -9
        stdout = ""
        stderr = "Killed"

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: Finished())

    with pytest.raises(RuntimeError, match="produced nothing"):
        FasterWhisperTranscriber(settings).transcribe(Path("big.wav"))
