"""M1 (audio) - turning a recording into segments the rest of the pipeline can read.

The transcriber is a parser. It produces `RawSegment`s exactly as the txt, vtt
and json parsers do, and everything after it is unchanged: the same validation,
the same normalisation, the same character offsets, the same quote
verification. Adding audio therefore adds one input, not one pipeline.

Two things about a machine transcript have to be said out loud, because both
change what a quote from it means.

**There are no speaker labels.** Whisper returns text and timings, not who was
talking. Diarisation is out of scope in the brief, so every segment carries
`speaker = None`, which the store reads as UNSPECIFIED. The consequence is
real: an owner can only be extracted where somebody is named aloud in the
words. It is not a gap to be filled in later from context, because filling it
in would be inventing an attribution.

**The words are the model's best guess.** Transcribing a short test clip on
this machine turned "Nuwan" into "new one" and "I am blocked" into
"I unblocked". A verbatim quote from an audio source is faithful to the
transcript and not necessarily to the room, so every audio ingestion carries a
warning saying so and the source records which model produced it.

Two implementations behind one interface, the same shape as the model
providers: faster-whisper for real use, and a deterministic stub so the test
suite never downloads a 140MB model or depends on a machine having audio
libraries.
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from pathlib import Path

from app.config import Settings, get_settings
from app.models.common import StrictModel
from app.models.ingestion import RawSegment

logger = logging.getLogger("agent.audio")

#: Extensions the pipeline will attempt. Anything else is refused by name
#: rather than handed to a decoder to fail on.
AUDIO_EXTENSIONS = {".wav", ".mp3", ".m4a", ".aiff", ".aif", ".flac", ".ogg", ".webm", ".mp4"}


def looks_like_audio(path: Path | str) -> bool:
    return Path(path).suffix.lower() in AUDIO_EXTENSIONS


def format_timestamp(seconds: float) -> str:
    """HH:MM:SS, the same shape the txt parser produces.

    Audio and text transcripts then read identically downstream, and a citation
    from either points at a timestamp a person can scrub to.
    """
    total = max(0, int(seconds))
    return f"{total // 3600:02d}:{(total % 3600) // 60:02d}:{total % 60:02d}"


class Transcription(StrictModel):
    """What a transcriber returns, before the store gives it identity."""

    segments: list[RawSegment] = []
    language: str | None = None
    duration_seconds: float | None = None
    model_name: str = "unknown"
    provider: str = "unknown"
    latency_ms: int = 0


class Transcriber(ABC):
    """One recording in, segments out. No storage, no consent logic."""

    name: str
    model: str

    @abstractmethod
    def available(self) -> tuple[bool, str]:
        """Whether this can run here, and a readable reason when it cannot."""

    @abstractmethod
    def transcribe(self, path: Path) -> Transcription:
        """Decode and transcribe. Raises only for genuine failure."""

    def describe(self) -> str:
        return f"{self.name}:{self.model}"


class FasterWhisperTranscriber(Transcriber):
    """faster-whisper, CPU, int8 by default, in a process of its own.

    The subprocess is not an implementation detail to hide. faiss and
    ctranslate2 each link their own OpenMP runtime, and loading both into one
    process aborts on macOS with `OMP: Error #15`. The API loads faiss for
    retrieval, so it cannot also load whisper. Measured on this machine: a
    faiss search followed by a whisper load in one process kills the process.

    The documented workaround, `KMP_DUPLICATE_LIB_OK=TRUE`, is described by the
    runtime itself as unsafe and capable of silently producing incorrect
    results. This build refuses silently incorrect everywhere else, so it is
    refused here.

    The cost is a few seconds of startup for a file somebody uploaded by hand,
    and the benefit beyond correctness is that a decoder failure kills a worker
    rather than the API.
    """

    name = "whisper"

    #: Generous, because a first run downloads the model. A ten-minute
    #: recording on the base model takes well under a minute after that.
    timeout_seconds = 900

    def __init__(self, settings: Settings | None = None) -> None:
        cfg = settings or get_settings()
        self.model = cfg.whisper_model
        self._compute_type = cfg.whisper_compute_type

    def available(self) -> tuple[bool, str]:
        """Probed without importing, on purpose.

        Importing faster-whisper pulls in ctranslate2 and PyAV, each with its
        own native OpenMP runtime. Loading those alongside faiss aborts the
        process on macOS, and an availability check has no business
        initialising anything. find_spec answers the question without paying
        for it.
        """
        from importlib.util import find_spec

        if find_spec("faster_whisper") is None:
            return False, (
                "faster-whisper is not installed. Run `make setup`, or set "
                "AUDIO_PROVIDER=fake to exercise the path without it."
            )
        return True, f"whisper {self.model} on cpu, {self._compute_type}"

    def transcribe(self, path: Path) -> Transcription:
        import json
        import subprocess
        import sys

        worker = Path(__file__).with_name("whisper_worker.py")
        started = time.perf_counter()

        logger.info("transcribing %s with whisper %s in a worker", path.name, self.model)
        finished = subprocess.run(
            [sys.executable, str(worker), str(path), self.model, self._compute_type],
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
        )

        if not finished.stdout.strip():
            raise RuntimeError(
                f"the transcription worker produced nothing "
                f"(exit {finished.returncode}): {finished.stderr.strip()[-300:]}"
            )

        payload = json.loads(finished.stdout.strip().splitlines()[-1])
        if "error" in payload:
            raise RuntimeError(payload["error"])

        segments = [
            RawSegment(
                speaker=None,          # whisper does not diarise, and nothing here guesses
                start_ts=format_timestamp(chunk["start"]),
                end_ts=format_timestamp(chunk["end"]),
                start_seconds=float(chunk["start"]),
                text=chunk["text"],
            )
            for chunk in payload["segments"]
        ]

        return Transcription(
            segments=segments,
            language=payload.get("language"),
            duration_seconds=payload.get("duration"),
            model_name=self.model,
            provider=self.name,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )


class FakeTranscriber(Transcriber):
    """A deterministic stub, so the suite never downloads a model.

    It reads a sidecar file of the same name with a .txt extension when one
    exists, which lets a test state exactly what was 'heard' without shipping
    audio. Otherwise it returns one fixed segment.

    Real behaviour is preserved where it matters: no speaker labels, real
    timestamps, one segment per line.
    """

    name = "fake"
    model = "stub"

    def available(self) -> tuple[bool, str]:
        return True, "deterministic stub, no model loaded"

    def transcribe(self, path: Path) -> Transcription:
        sidecar = Path(path).with_suffix(".txt")
        lines = (
            [line.strip() for line in sidecar.read_text(encoding="utf-8").splitlines() if line.strip()]
            if sidecar.exists()
            else ["This is a stub transcription and measures nothing about a model."]
        )
        segments = [
            RawSegment(
                speaker=None,
                start_ts=format_timestamp(index * 5),
                end_ts=format_timestamp(index * 5 + 5),
                start_seconds=float(index * 5),
                text=line,
            )
            for index, line in enumerate(lines)
        ]
        return Transcription(
            segments=segments,
            language="en",
            duration_seconds=float(len(segments) * 5),
            model_name=self.model,
            provider=self.name,
            latency_ms=0,
        )


def get_transcriber(settings: Settings | None = None) -> Transcriber:
    """The one place a concrete transcriber is named."""
    cfg = settings or get_settings()
    if cfg.audio_provider == "fake":
        return FakeTranscriber()
    return FasterWhisperTranscriber(cfg)
