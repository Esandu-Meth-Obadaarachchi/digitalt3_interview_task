"""Contracts for ingested sources and their normalised segments (M1, M2)."""

from __future__ import annotations

from datetime import datetime

from pydantic import AliasChoices, ConfigDict, Field, field_validator

from app.models.common import SourceStatus, SourceType, StrictModel
from app.models.ingestion import ConsentDecision, Defect


class SourceMetadata(StrictModel):
    """The metadata block supplied alongside a source file.

    `consent_flag` has no default on purpose. The brief says "refuse to
    process any source whose consent flag is not explicitly true", so a
    missing flag must be a validation failure rather than a silent default
    in either direction.
    """

    # Supplied metadata is an external shape, so the contract accepts the field
    # spellings a real export would plausibly use and normalises them to one
    # internal name. The alias list is the only place that mapping lives.
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, populate_by_name=True)

    id: str
    title: str
    source_type: SourceType
    consent_flag: bool = Field(validation_alias=AliasChoices("consent_flag", "consent"))
    meeting_date: str | None = Field(
        default=None, validation_alias=AliasChoices("meeting_date", "date")
    )
    participants: list[str] = Field(default_factory=list)
    file_path: str | None = None

    @field_validator("meeting_date")
    @classmethod
    def _check_iso_date(cls, value: str | None) -> str | None:
        if value is None:
            return None
        datetime.strptime(value, "%Y-%m-%d")  # raises on a malformed date
        return value


class Segment(StrictModel):
    """One normalised transcript line.

    char_start/char_end are offsets into the whitespace-normalised full text
    of the source. Quote verification and citation spans both work against
    that same normalised text, so an offset here is directly checkable.
    """

    id: str
    source_id: str
    segment_index: int
    speaker: str | None = None
    start_ts: str | None = None
    end_ts: str | None = None
    start_seconds: float | None = None
    text: str
    char_start: int
    char_end: int


class Source(StrictModel):
    """A stored source, including the refused and errored ones.

    A refusal is a record rather than a log line: the demo has to show that
    the non-consented meeting was seen, refused, and provably not processed.
    """

    id: str
    title: str
    source_type: SourceType
    meeting_date: str | None = None
    participants: list[str] = Field(default_factory=list)
    consent_flag: bool
    origin_format: str | None = None
    file_path: str | None = None
    content_hash: str | None = None
    ingested_at: str
    status: SourceStatus
    refusal_reason: str | None = None
    error_detail: str | None = None


class IngestionReport(StrictModel):
    """What ingestion actually did, kept so the demo can prove its claims.

    `bytes_read` is the evidence for the consent gate: a refused source shows
    zero, because the file was never opened.

    `direct_messages_excluded` is the only surviving trace that a DM was seen
    at all, since the schema makes storing one impossible.

    `silent_participants` names people listed in the metadata who never speak,
    which stops a reviewer wondering whether the parser lost them.
    """

    source_id: str
    ok: bool
    status: SourceStatus
    consent: ConsentDecision | None = None
    #: True when the file was byte-identical to what is already stored and
    #: nothing was rewritten. Re-ingesting is then a genuine no-op.
    unchanged: bool = False

    origin_format: str | None = None
    encoding: str | None = None
    bytes_read: int = 0
    content_hash: str | None = None

    segments_parsed: int = 0
    messages_parsed: int = 0
    direct_messages_excluded: int = 0
    speakers: list[str] = Field(default_factory=list)
    silent_participants: list[str] = Field(default_factory=list)
    duration_seconds: float | None = None

    defects: list[Defect] = Field(default_factory=list)
    rejection_reason: str | None = None

    @property
    def blocking_defects(self) -> list[Defect]:
        return [d for d in self.defects if d.blocking]

    @property
    def warnings(self) -> list[Defect]:
        return [d for d in self.defects if not d.blocking]
