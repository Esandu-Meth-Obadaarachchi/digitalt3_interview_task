"""Cross-cutting validation applied to every parsed source, whatever its format.

Severity is graded rather than binary. The brief requires the deliberately
malformed sample be "rejected with a clear reason" and must not "corrupt the
store", but a transcript with one unlabelled line is realistic input and
rejecting it whole would be heavy-handed.

  ERROR   nothing is stored, the source is recorded with status='error' and
          the reason is kept against it
  WARNING the source is stored, the defect travels with it and is visible in
          the review interface
"""

from __future__ import annotations

from app.models.ingestion import Defect, DefectCode, DefectSeverity, ParseResult

#: A final segment ending in one of these reads as a complete thought.
_TERMINAL_PUNCTUATION = ('.', '!', '?', '"', "'", ")", "]", "…", "”", "’")

#: Below this many segments a "transcript" is not a meeting.
_MIN_SEGMENTS = 2


def validate(result: ParseResult, *, check_truncation: bool = True) -> ParseResult:
    """Return the result with any additional defects appended."""
    defects = list(result.defects)

    if not result.raw_segments:
        if not any(d.blocking for d in defects):
            defects.append(
                Defect(
                    code=DefectCode.NO_PARSEABLE_SEGMENTS,
                    severity=DefectSeverity.ERROR,
                    detail="no segment could be parsed from this file",
                )
            )
        return result.model_copy(update={"defects": defects})

    if len(result.raw_segments) < _MIN_SEGMENTS:
        defects.append(
            Defect(
                code=DefectCode.NO_PARSEABLE_SEGMENTS,
                severity=DefectSeverity.ERROR,
                detail=(
                    f"only {len(result.raw_segments)} segment parsed, which is below the "
                    f"minimum of {_MIN_SEGMENTS}. This is usually the wrong parser for the file."
                ),
            )
        )

    if check_truncation:
        defects.extend(_check_truncation(result))

    defects.extend(_check_monotonic_timestamps(result))

    return result.model_copy(update={"defects": defects})


def _check_truncation(result: ParseResult) -> list[Defect]:
    """Detect a file that stops mid-sentence.

    Heuristic, deliberately: the last segment does not end in terminal
    punctuation. On the committed samples this separates the malformed file
    ("We'd need to upd") from the three valid ones, which all end on a full
    stop. It is a heuristic, so it is documented as one in decision_log.md and
    is switchable via `check_truncation` for a source genuinely known to end
    mid-thought.
    """
    last = result.raw_segments[-1]
    text = last.text.rstrip()

    if not text or text.endswith(_TERMINAL_PUNCTUATION):
        return []

    tail = text.split()[-1] if text.split() else text
    return [
        Defect(
            code=DefectCode.TRUNCATED_MID_SENTENCE,
            severity=DefectSeverity.ERROR,
            detail=(
                f"the file ends without terminal punctuation, on {tail!r}. The recording "
                f"appears to have been cut off, so quotes drawn from the end of it would be "
                f"incomplete and any extraction from it unreliable."
            ),
            line_number=last.line_number,
            excerpt=text[-120:],
        )
    ]


def _check_monotonic_timestamps(result: ParseResult) -> list[Defect]:
    """Timestamps that go backwards mean the ordering cannot be trusted."""
    defects: list[Defect] = []
    previous: float | None = None
    previous_ts: str | None = None

    for segment in result.raw_segments:
        if segment.start_seconds is None:
            continue
        if previous is not None and segment.start_seconds < previous:
            defects.append(
                Defect(
                    code=DefectCode.NON_MONOTONIC_TIMESTAMP,
                    severity=DefectSeverity.WARNING,
                    detail=(
                        f"timestamp {segment.start_ts} goes backwards from {previous_ts}, "
                        f"so segment order may not match the order of speech"
                    ),
                    line_number=segment.line_number,
                    excerpt=segment.text[:120],
                )
            )
        previous = segment.start_seconds
        previous_ts = segment.start_ts

    return defects


def summarise(result: ParseResult) -> str:
    """One line naming why a source was rejected, for the refusal record."""
    blocking = result.blocking_defects
    if not blocking:
        return ""
    head = blocking[0]
    location = f" at line {head.line_number}" if head.line_number else ""
    extra = f" (and {len(blocking) - 1} further blocking defect(s))" if len(blocking) > 1 else ""
    return f"{head.code.value}{location}: {head.detail}{extra}"
