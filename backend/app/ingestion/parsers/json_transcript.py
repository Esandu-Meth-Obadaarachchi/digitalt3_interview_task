"""JSON transcript parser (M1).

Speech-to-text tools and meeting platforms all emit JSON, and none of them
agree on the key names. The accepted spellings are listed once, here, so the
rest of the system sees one shape. Accepted containers:

    {"segments": [...]}   {"transcript": [...]}   {"utterances": [...]}   [...]

Accepted keys per entry:

    speaker  speaker | speaker_name | spk | name | participant
    start    start | start_ts | start_time | begin | timestamp | ts | offset
    end      end | end_ts | end_time | stop
    text     text | content | utterance | body | transcript
"""

from __future__ import annotations

import json
from typing import Any

from app.ingestion.parsers.speakers import build_speaker_lookup
from app.ingestion.timestamps import normalise_timestamp
from app.models.ingestion import Defect, DefectCode, DefectSeverity, ParseResult, RawSegment, TranscriptFormat

_CONTAINERS = ("segments", "transcript", "utterances", "results", "lines")
_SPEAKER_KEYS = ("speaker", "speaker_name", "spk", "name", "participant")
_START_KEYS = ("start", "start_ts", "start_time", "begin", "timestamp", "ts", "offset")
_END_KEYS = ("end", "end_ts", "end_time", "stop")
_TEXT_KEYS = ("text", "content", "utterance", "body", "transcript")


def _first(entry: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in entry and entry[key] not in (None, ""):
            return entry[key]
    return None


def _locate_entries(document: Any) -> tuple[list[Any] | None, str | None]:
    if isinstance(document, list):
        return document, None
    if isinstance(document, dict):
        for container in _CONTAINERS:
            if isinstance(document.get(container), list):
                return document[container], None
        return None, (
            "no list of segments found. Expected one of "
            f"{', '.join(_CONTAINERS)} at the top level, or a top-level list"
        )
    return None, f"top level is {type(document).__name__}, expected an object or a list"


def parse(text: str, encoding: str, bytes_read: int, participants: list[str] | None = None) -> ParseResult:
    lookup = build_speaker_lookup(participants)
    defects: list[Defect] = []

    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        return ParseResult(
            origin_format=TranscriptFormat.JSON,
            encoding=encoding,
            bytes_read=bytes_read,
            defects=[
                Defect(
                    code=DefectCode.MALFORMED_STRUCTURE,
                    severity=DefectSeverity.ERROR,
                    detail=f"invalid JSON: {exc.msg}",
                    line_number=exc.lineno,
                    excerpt=text.splitlines()[exc.lineno - 1][:120] if 0 < exc.lineno <= len(text.splitlines()) else None,
                )
            ],
        )

    entries, problem = _locate_entries(document)
    if entries is None:
        return ParseResult(
            origin_format=TranscriptFormat.JSON,
            encoding=encoding,
            bytes_read=bytes_read,
            defects=[
                Defect(
                    code=DefectCode.MALFORMED_STRUCTURE,
                    severity=DefectSeverity.ERROR,
                    detail=problem or "unrecognised JSON transcript structure",
                )
            ],
        )

    segments: list[RawSegment] = []
    for position, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            defects.append(
                Defect(
                    code=DefectCode.MALFORMED_STRUCTURE,
                    severity=DefectSeverity.WARNING,
                    detail=f"entry {position} is {type(entry).__name__}, expected an object",
                    line_number=position,
                )
            )
            continue

        body = _first(entry, _TEXT_KEYS)
        if not body or not str(body).strip():
            defects.append(
                Defect(
                    code=DefectCode.EMPTY_SEGMENT_TEXT,
                    severity=DefectSeverity.WARNING,
                    detail=f"entry {position} carries no text under any recognised key",
                    line_number=position,
                )
            )
            continue

        speaker = _first(entry, _SPEAKER_KEYS)
        speaker = str(speaker).strip() if speaker else None
        if speaker and lookup:
            import re as _re

            speaker = lookup.get(_re.sub(r"[^a-z]", "", speaker.lower()), speaker)

        start_ts, start_seconds = normalise_timestamp(_first(entry, _START_KEYS))
        end_ts, _ = normalise_timestamp(_first(entry, _END_KEYS))

        if speaker is None:
            defects.append(
                Defect(
                    code=DefectCode.MISSING_SPEAKER_LABEL,
                    severity=DefectSeverity.WARNING,
                    detail=f"entry {position} names no speaker, left unattributed",
                    line_number=position,
                    excerpt=str(body)[:120],
                )
            )
        if start_ts is None:
            defects.append(
                Defect(
                    code=DefectCode.MISSING_TIMESTAMP,
                    severity=DefectSeverity.WARNING,
                    detail=f"entry {position} carries no readable start time",
                    line_number=position,
                )
            )

        segments.append(
            RawSegment(
                speaker=speaker,
                start_ts=start_ts,
                end_ts=end_ts,
                start_seconds=start_seconds,
                text=" ".join(str(body).split()),
                line_number=position,
            )
        )

    if not segments:
        defects.append(
            Defect(
                code=DefectCode.NO_PARSEABLE_SEGMENTS,
                severity=DefectSeverity.ERROR,
                detail=f"{len(entries)} entries found but none carried usable text",
            )
        )

    return ParseResult(
        origin_format=TranscriptFormat.JSON,
        encoding=encoding,
        bytes_read=bytes_read,
        raw_segments=segments,
        defects=defects,
    )
