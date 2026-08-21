"""M11 - the versioned outcome record.

    "One versioned outcome record per source, written to a documented location
     with a published schema, consumable by a delivery agent without reading
     raw transcripts."

The test that shaped this: a second process must be able to read one record and
reconstruct the approved items with no access to the transcript store. So
everything a consumer needs is inside the file. Nothing is a foreign key into a
database they do not have.

That means each item carries its own quote, speaker, timestamp and source id
rather than an extraction id to look up. The record is larger for it, and that
is the point: a record that requires this application to interpret is not a
record a delivery agent can consume.

`consent_flag` is carried forward because consent travels with the content. A
downstream agent receiving these items has no other way to know whether the
meeting they came from permitted processing at all.
"""

from __future__ import annotations

from pydantic import Field

from app.models.common import StrictModel

#: Bumped when the shape changes in a way a consumer would notice. Consumers
#: are expected to check it, which is why it is a field rather than a filename
#: convention.
SCHEMA_VERSION = "1.0"


class OutcomeCitation(StrictModel):
    """Everything needed to check one item, without the transcript store."""

    source_id: str
    source_title: str | None = None
    speaker: str | None = None
    timestamp: str | None = None
    quote: str
    quote_verified: bool
    char_start: int | None = None
    char_end: int | None = None


class OutcomeItem(StrictModel):
    """One approved extraction, flattened for a consumer.

    `approved_by` and `approved_at` travel with the item because the value of
    this record is that a human accepted each line in it, and a downstream
    agent acting on one should be able to say who accepted it.
    """

    id: str
    type: str                       # action | decision | risk | signal
    payload: dict
    citation: OutcomeCitation
    confidence: float | None = None
    approved_by: str | None = None
    approved_at: str | None = None
    edited_by_reviewer: bool = False


class OutcomeRecord(StrictModel):
    """The artefact a delivery agent consumes."""

    schema_version: str = SCHEMA_VERSION
    record_version: int
    record_id: str

    source_id: str
    source_title: str
    source_type: str
    meeting_date: str | None = None
    participants: list[str] = Field(default_factory=list)

    #: Carried forward deliberately. A consumer has no other way to know
    #: whether the meeting these items came from permitted processing.
    consent_flag: bool

    generated_at: str
    generated_by: str = "meeting-intelligence-agent"

    actions: list[OutcomeItem] = Field(default_factory=list)
    decisions: list[OutcomeItem] = Field(default_factory=list)
    risks: list[OutcomeItem] = Field(default_factory=list)
    signals: list[OutcomeItem] = Field(default_factory=list)

    #: Counts of what was NOT included, so a consumer can tell "nothing was
    #: found" from "nothing has been reviewed yet". Without this, an empty
    #: record is ambiguous in a way that matters.
    pending_not_included: int = 0
    rejected_not_included: int = 0
    expired_not_included: int = 0

    @property
    def total_items(self) -> int:
        return len(self.actions) + len(self.decisions) + len(self.risks) + len(self.signals)

    @property
    def all_items(self) -> list[OutcomeItem]:
        return [*self.actions, *self.decisions, *self.risks, *self.signals]
