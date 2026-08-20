"""WebVTT subtitle parser (M1).

Handles both speaker conventions found in the wild:

    WEBVTT

    1
    00:00:05.000 --> 00:00:27.000
    <v Sarah Chen>Alright, let's get started.

    00:00:28.000 --> 00:00:38.000
    Priya Sharma: Morning everyone.

Cue identifiers, NOTE blocks and STYLE blocks are skipped. Inline tags other
than the voice tag are stripped, because a stored quote must be the words
that were said and nothing else, or the substring check against it is
meaningless.
"""

from __future__ import annotations

import re

from app.ingestion.parsers.speakers import build_speaker_lookup, split_speaker
from app.ingestion.timestamps import normalise_timestamp
from app.models.ingestion import Defect, DefectCode, DefectSeverity, ParseResult, RawSegment, TranscriptFormat

_CUE_TIMING = re.compile(r"^(?P<start>[\d:.,]+)\s*-->\s*(?P<end>[\d:.,]+)")
_VOICE_TAG = re.compile(r"<v(?:\.[^\s>]+)*\s+(?P<speaker>[^>]+)>", re.IGNORECASE)
_ANY_TAG = re.compile(r"</?[^>]+>")


def parse(text: str, encoding: str, bytes_read: int, participants: list[str] | None = None) -> ParseResult:
    lookup = build_speaker_lookup(participants)
    segments: list[RawSegment] = []
    defects: list[Defect] = []

    lines = text.splitlines()

    if not lines or not lines[0].lstrip().upper().startswith("WEBVTT"):
        defects.append(
            Defect(
                code=DefectCode.MALFORMED_STRUCTURE,
                severity=DefectSeverity.ERROR,
                detail="a WebVTT file must begin with the WEBVTT signature",
                line_number=1,
                excerpt=(lines[0][:120] if lines else ""),
            )
        )
        return ParseResult(
            origin_format=TranscriptFormat.VTT, encoding=encoding, bytes_read=bytes_read, defects=defects
        )

    index = 1
    while index < len(lines):
        line = lines[index].strip()

        if not line or line.startswith(("NOTE", "STYLE", "REGION")):
            index += 1
            continue

        timing = _CUE_TIMING.match(line)
        if timing is None:
            # A cue identifier sits on the line before the timing line.
            index += 1
            continue

        cue_line_number = index + 1
        start_ts, start_seconds = normalise_timestamp(timing.group("start"))
        end_ts, _ = normalise_timestamp(timing.group("end"))

        payload_lines: list[str] = []
        index += 1
        while index < len(lines) and lines[index].strip():
            payload_lines.append(lines[index].strip())
            index += 1

        payload = " ".join(payload_lines).strip()
        if not payload:
            defects.append(
                Defect(
                    code=DefectCode.EMPTY_SEGMENT_TEXT,
                    severity=DefectSeverity.WARNING,
                    detail="cue carries timing but no text",
                    line_number=cue_line_number,
                )
            )
            continue

        speaker: str | None = None
        voice = _VOICE_TAG.search(payload)
        if voice:
            speaker = voice.group("speaker").strip()
            payload = _VOICE_TAG.sub("", payload)

        payload = _ANY_TAG.sub("", payload).strip()

        if speaker is None:
            speaker, payload = split_speaker(payload, lookup)
        elif lookup:
            speaker = lookup.get(re.sub(r"[^a-z]", "", speaker.lower()), speaker)

        if speaker is None:
            defects.append(
                Defect(
                    code=DefectCode.MISSING_SPEAKER_LABEL,
                    severity=DefectSeverity.WARNING,
                    detail="cue carries no voice tag and no speaker prefix, left unattributed",
                    line_number=cue_line_number,
                    excerpt=payload[:120],
                )
            )

        if start_ts is None:
            defects.append(
                Defect(
                    code=DefectCode.MISSING_TIMESTAMP,
                    severity=DefectSeverity.WARNING,
                    detail=f"cue timing {timing.group('start')!r} is not readable",
                    line_number=cue_line_number,
                )
            )

        segments.append(
            RawSegment(
                speaker=speaker,
                start_ts=start_ts,
                end_ts=end_ts,
                start_seconds=start_seconds,
                text=payload,
                line_number=cue_line_number,
            )
        )

    if not segments and not any(d.blocking for d in defects):
        defects.append(
            Defect(
                code=DefectCode.NO_PARSEABLE_SEGMENTS,
                severity=DefectSeverity.ERROR,
                detail="the WEBVTT signature is present but no cue could be parsed",
            )
        )

    return ParseResult(
        origin_format=TranscriptFormat.VTT,
        encoding=encoding,
        bytes_read=bytes_read,
        raw_segments=segments,
        defects=defects,
    )
