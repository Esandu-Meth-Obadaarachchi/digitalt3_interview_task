"""Plain-text transcript parser (M1).

Handles the shape the sample transcripts use:

    [00:00:05] Sarah Chen: Alright, let's get started.
    [00:02:04] We'd need to update the state machine.      <- no speaker label
    Sarah Chen: Alright, let's get started.                <- no timestamp
    Alright, let's get started.                            <- neither

A line with no speaker label produces a segment with speaker=None. It is never
attributed to the previous speaker, because the previous speaker is a guess.
"""

from __future__ import annotations

import re

from app.ingestion.parsers.speakers import build_speaker_lookup, split_speaker
from app.ingestion.timestamps import normalise_timestamp
from app.models.ingestion import Defect, DefectCode, DefectSeverity, ParseResult, RawSegment, TranscriptFormat

_TIMESTAMPED = re.compile(r"^\s*[\[(<]\s*(?P<ts>[^\])>]{1,20})\s*[\])>]\s*(?P<rest>.*)$")


def parse(text: str, encoding: str, bytes_read: int, participants: list[str] | None = None) -> ParseResult:
    lookup = build_speaker_lookup(participants)
    segments: list[RawSegment] = []
    defects: list[Defect] = []

    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue

        start_ts: str | None = None
        start_seconds: float | None = None
        body = line.strip()

        match = _TIMESTAMPED.match(line)
        if match:
            start_ts, start_seconds = normalise_timestamp(match.group("ts"))
            body = match.group("rest").strip()
            if start_ts is None:
                defects.append(
                    Defect(
                        code=DefectCode.MISSING_TIMESTAMP,
                        severity=DefectSeverity.WARNING,
                        detail=f"bracketed value {match.group('ts')!r} is not a readable timestamp",
                        line_number=line_number,
                        excerpt=line.strip()[:120],
                    )
                )
        else:
            defects.append(
                Defect(
                    code=DefectCode.MISSING_TIMESTAMP,
                    severity=DefectSeverity.WARNING,
                    detail="line carries no timestamp, so any citation to it has no time anchor",
                    line_number=line_number,
                    excerpt=line.strip()[:120],
                )
            )

        speaker, body = split_speaker(body, lookup)

        if not body:
            defects.append(
                Defect(
                    code=DefectCode.EMPTY_SEGMENT_TEXT,
                    severity=DefectSeverity.WARNING,
                    detail="line has a timestamp or a speaker but no speech",
                    line_number=line_number,
                    excerpt=line.strip()[:120],
                )
            )
            continue

        if speaker is None:
            defects.append(
                Defect(
                    code=DefectCode.MISSING_SPEAKER_LABEL,
                    severity=DefectSeverity.WARNING,
                    detail=(
                        "no speaker label on this line. Left unattributed rather than "
                        "inherited from the line above, which would invent a speaker."
                    ),
                    line_number=line_number,
                    excerpt=body[:120],
                )
            )

        segments.append(
            RawSegment(
                speaker=speaker,
                start_ts=start_ts,
                start_seconds=start_seconds,
                text=body,
                line_number=line_number,
            )
        )

    return ParseResult(
        origin_format=TranscriptFormat.TXT,
        encoding=encoding,
        bytes_read=bytes_read,
        raw_segments=segments,
        defects=defects,
    )
