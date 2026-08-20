"""Format detection and dispatch.

Adding a fifth transcript format means writing one module with a `parse`
function and adding one entry to `_PARSERS`. Nothing else in the system
changes, because everything downstream consumes `ParseResult`.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

from app.ingestion.parsers import json_transcript, txt, vtt
from app.models.ingestion import Defect, DefectCode, DefectSeverity, ParseResult, TranscriptFormat

ParserFn = Callable[[str, str, int, list[str] | None], ParseResult]

_PARSERS: dict[TranscriptFormat, ParserFn] = {
    TranscriptFormat.TXT: txt.parse,
    TranscriptFormat.VTT: vtt.parse,
    TranscriptFormat.JSON: json_transcript.parse,
}

_SUFFIXES: dict[str, TranscriptFormat] = {
    ".txt": TranscriptFormat.TXT,
    ".md": TranscriptFormat.TXT,
    ".log": TranscriptFormat.TXT,
    ".vtt": TranscriptFormat.VTT,
    ".webvtt": TranscriptFormat.VTT,
    ".srt": TranscriptFormat.VTT,
    ".json": TranscriptFormat.JSON,
}

AUDIO_SUFFIXES = {".wav", ".mp3", ".m4a", ".aiff", ".aif", ".flac", ".ogg", ".mp4", ".webm"}

#: "[00:00:05] Speaker: ..." - a plain transcript, not a JSON array.
_TXT_TIMESTAMP_HEAD = re.compile(r"^[\[(<]\s*\d{1,2}:\d{2}")


def detect_format(path: Path, text: str = "") -> TranscriptFormat | None:
    """Suffix first, then a content sniff.

    The sniff matters because an uploaded file often arrives with the wrong
    extension, and a wrong parser produces a confident wrong answer rather
    than an error.
    """
    suffix = path.suffix.lower()

    if suffix in AUDIO_SUFFIXES:
        return TranscriptFormat.AUDIO

    sniffed: TranscriptFormat | None = None
    stripped = text.lstrip()
    if stripped.upper().startswith("WEBVTT"):
        sniffed = TranscriptFormat.VTT
    elif _TXT_TIMESTAMP_HEAD.match(stripped):
        # A plain transcript opens on a bracketed timestamp, "[00:00:05] ...".
        # Checked before the JSON sniff, because that leading bracket otherwise
        # reads as the start of a JSON array.
        sniffed = TranscriptFormat.TXT
    elif stripped[:1] in {"{", "["}:
        sniffed = TranscriptFormat.JSON

    by_suffix = _SUFFIXES.get(suffix)

    # Content wins over an extension it contradicts: a .txt holding WEBVTT is
    # a VTT file that was renamed, and parsing it as prose loses every cue.
    if sniffed is not None and by_suffix in (None, TranscriptFormat.TXT):
        return sniffed
    return by_suffix or sniffed


def parse_transcript(
    path: Path,
    text: str,
    encoding: str,
    bytes_read: int,
    participants: list[str] | None = None,
    fmt: TranscriptFormat | None = None,
) -> ParseResult:
    resolved = fmt or detect_format(path, text)

    if resolved is None:
        return ParseResult(
            origin_format=TranscriptFormat.TXT,
            encoding=encoding,
            bytes_read=bytes_read,
            defects=[
                Defect(
                    code=DefectCode.UNKNOWN_FORMAT,
                    severity=DefectSeverity.ERROR,
                    detail=(
                        f"cannot tell what {path.name} is. Supported transcript formats: "
                        f"{', '.join(sorted(s for s in _SUFFIXES))}"
                    ),
                )
            ],
        )

    if resolved is TranscriptFormat.AUDIO:
        return ParseResult(
            origin_format=TranscriptFormat.AUDIO,
            encoding=encoding,
            bytes_read=bytes_read,
            defects=[
                Defect(
                    code=DefectCode.UNKNOWN_FORMAT,
                    severity=DefectSeverity.ERROR,
                    detail="audio must go through the transcription path, not the transcript parsers",
                )
            ],
        )

    return _PARSERS[resolved](text, encoding, bytes_read, participants)


__all__ = ["detect_format", "parse_transcript", "AUDIO_SUFFIXES"]
