"""Contracts for parsing and validating a source file (M1).

The parsers are a format concern and know nothing about storage: they return
`RawSegment` objects and a list of `Defect` objects. Assigning identifiers,
indices and character offsets is the ingestion service's job. Keeping the two
apart is why adding a fourth transcript format costs one file.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from app.models.common import StrictModel


class TranscriptFormat(StrEnum):
    TXT = "txt"
    VTT = "vtt"
    JSON = "json"
    AUDIO = "audio"


class DefectSeverity(StrEnum):
    """Whether a defect blocks ingestion or is merely recorded.

    ERROR   the file cannot be trusted as a whole, so nothing is stored and
            the source is recorded with status='error'
    WARNING the file is usable, the defect is recorded against the source and
            surfaced in the review interface
    """

    ERROR = "error"
    WARNING = "warning"


class DefectCode(StrEnum):
    # Blocking
    EMPTY_FILE = "empty_file"
    UNDECODABLE_BYTES = "undecodable_bytes"
    NO_PARSEABLE_SEGMENTS = "no_parseable_segments"
    TRUNCATED_MID_SENTENCE = "truncated_mid_sentence"
    UNKNOWN_FORMAT = "unknown_format"
    MALFORMED_STRUCTURE = "malformed_structure"

    # Non-blocking
    MISSING_SPEAKER_LABEL = "missing_speaker_label"
    MISSING_TIMESTAMP = "missing_timestamp"
    NON_MONOTONIC_TIMESTAMP = "non_monotonic_timestamp"
    EMPTY_SEGMENT_TEXT = "empty_segment_text"
    REPLACEMENT_CHARACTERS = "replacement_characters"


class Defect(StrictModel):
    """One problem found in a source file, located and explained.

    `excerpt` exists so a human reading the rejection sees the offending text
    and agrees with the verdict, rather than being told a file was rejected
    and having to go and find out why.
    """

    code: DefectCode
    severity: DefectSeverity
    detail: str
    line_number: int | None = None
    excerpt: str | None = None

    @property
    def blocking(self) -> bool:
        return self.severity is DefectSeverity.ERROR


class RawSegment(StrictModel):
    """A parsed line, before the store assigns it an identity.

    `speaker` is None when the source did not label it. It is never filled in
    from the preceding line: rule 1 of the brief forbids inventing a speaker,
    and the line above is only a plausible guess.
    """

    speaker: str | None = None
    start_ts: str | None = None
    end_ts: str | None = None
    start_seconds: float | None = None
    text: str
    line_number: int | None = None


class ParseResult(StrictModel):
    """Everything a parser returns."""

    origin_format: TranscriptFormat
    encoding: str
    bytes_read: int
    raw_segments: list[RawSegment] = Field(default_factory=list)
    defects: list[Defect] = Field(default_factory=list)

    @property
    def blocking_defects(self) -> list[Defect]:
        return [d for d in self.defects if d.blocking]

    @property
    def ok(self) -> bool:
        return not self.blocking_defects and bool(self.raw_segments)


class ReadResult(StrictModel):
    """The outcome of reading a source file off disk."""

    text: str
    encoding: str
    bytes_read: int
    content_hash: str | None = None
    defects: list[Defect] = Field(default_factory=list)


class ConsentDecision(StrictModel):
    """M2. The consent verdict, recorded whether it passed or refused.

    Lives with the ingestion contracts rather than beside the gate itself so
    that both the gate and the stored report can refer to it without a cycle.
    """

    source_id: str
    granted: bool
    reason: str
    checked_at: str

    @property
    def refused(self) -> bool:
        return not self.granted
