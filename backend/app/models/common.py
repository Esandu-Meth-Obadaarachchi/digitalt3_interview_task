"""Shared vocabulary.

`UNSPECIFIED` is the most important value in this module. The brief names
silent guessing as "the single most damaging failure mode in a delivery-facing
agent, because it is fluent and therefore trusted". Making abstention an
explicit, valid, typed value is how the system is stopped from inventing an
owner or a date: there is always a correct thing to output, so the model is
never cornered into inventing one.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

#: Sentinel for "the source does not state this". Never inferred, never
#: substituted with a plausible value, and asserted on by golden cases 3 and 4.
UNSPECIFIED = "UNSPECIFIED"


class SourceType(StrEnum):
    TRANSCRIPT = "transcript"
    AUDIO = "audio"
    CHAT_EXPORT = "chat_export"


class SourceStatus(StrEnum):
    INGESTED = "ingested"
    REFUSED = "refused"      # consent gate, M2
    ERROR = "error"          # unparseable, M1


class ExtractionType(StrEnum):
    ACTION = "action"
    DECISION = "decision"
    RISK = "risk"
    SIGNAL = "signal"


class ReviewStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"      # safe default for an item nobody reviewed in time


class Severity(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class SignalClass(StrEnum):
    DECISION = "decision"
    BLOCKER = "blocker"
    QUESTION = "question"
    REQUEST = "request"
    NOISE = "noise"          # classified, then discarded rather than stored


class DateType(StrEnum):
    """How a due date came to have its value. Drives golden case 4."""

    ABSOLUTE = "absolute"        # the source stated a date
    RELATIVE_RESOLVED = "relative_resolved"  # source stated "end of next week", resolved against meeting_date
    UNSPECIFIED = "unspecified"  # the source stated nothing


Confidence = Annotated[float, Field(ge=0.0, le=1.0)]


class StrictModel(BaseModel):
    """Base for every model contract.

    `extra="forbid"` matters for LLM output: a model that invents a field is
    a model drifting from the schema, and the retry loop should see that as a
    validation failure rather than quietly ignoring it.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Citation(StrictModel):
    """A pointer precise enough to check by hand.

    The rubric's red flag is "citations that point at a document but not a
    location within it", so a citation carries the offsets of the quoted span
    as well as the source and timestamp.
    """

    source_id: str
    source_title: str | None = None
    segment_id: str | None = None
    message_id: str | None = None
    speaker: str | None = None
    timestamp: str | None = None
    quote: str
    char_start: int | None = None
    char_end: int | None = None
