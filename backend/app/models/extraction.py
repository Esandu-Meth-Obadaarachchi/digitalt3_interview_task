"""Contracts for extracted items (M3, M4, M5, M9).

Two layers, deliberately separate:

  Draft*      what the model returns. Shaped for the model, validated on the
              way in, and never stored directly.
  Extraction  what the store holds: the draft plus provenance, verification
              state, review state and the audit fields.

Keeping them apart means the model's schema can change without touching the
database, and the model is never handed fields it has no business inventing,
such as the review status or the character offsets of its own quote.
"""

from __future__ import annotations

from pydantic import Field, field_validator

from app.models.common import (
    UNSPECIFIED,
    Confidence,
    DateType,
    ExtractionType,
    ReviewStatus,
    Severity,
    SignalClass,
    StrictModel,
)


# =============================================================================
# What the model returns
# =============================================================================

#: The several ways a model spells "the source does not state this".
_ABSTENTIONS = {
    "", "unspecified", "not specified", "none", "n/a", "na", "null",
    "unknown", "not stated", "not given", "none given", "none stated",
    "not applicable", "no reason given", "no rationale given", "no owner",
    "not mentioned", "not discussed", "tbd", "unclear",
}


def _collapse_abstention(value: str) -> str:
    """Map every spelling of abstention onto the one token the system checks.

    Not guessing: it recognises synonyms of "not stated" and normalises them.
    Anything else is left exactly as the model wrote it.
    """
    cleaned = value.strip()
    return UNSPECIFIED if cleaned.lower().strip(".") in _ABSTENTIONS else cleaned


class DraftAction(StrictModel):
    """One action item as the model proposes it.

    Every field the model fills in is either copied from the transcript or
    stated as UNSPECIFIED. Nothing here is computed, inferred or looked up.
    """

    what: str = Field(description="the work to be done, in the model's words")
    owner: str = Field(description="who will do it, or UNSPECIFIED")
    due_date: str = Field(description="timing exactly as the transcript stated it, or UNSPECIFIED")
    verbatim_quote: str = Field(description="copied character for character from the chunk")
    speaker: str = Field(description="who said the quote")
    timestamp: str = Field(description="the timestamp of the quoted line")
    confidence: Confidence

    @field_validator("owner", "due_date", mode="after")
    @classmethod
    def _normalise_abstention(cls, value: str) -> str:
        return _collapse_abstention(value)


class DraftActionList(StrictModel):
    """The response schema for one chunk. An empty list is a valid answer."""

    actions: list[DraftAction] = Field(default_factory=list)


class DraftDecision(StrictModel):
    """One decision as the model proposes it (M4).

    `stated_rationale` and `who_stated_it` both accept UNSPECIFIED. A reason
    that sounds plausible but was not said is the same failure as an invented
    owner, and it is harder to spot because a decision log full of tidy
    rationales reads better than one that admits nobody gave a reason.
    """

    what_was_decided: str
    stated_rationale: str = Field(description="the reason the transcript gives, or UNSPECIFIED")
    who_stated_it: str = Field(description="who articulated the settled decision, or UNSPECIFIED")
    alternatives_discussed: list[str] = Field(
        default_factory=list, description="other options the transcript actually mentions"
    )
    verbatim_quote: str
    timestamp: str
    confidence: Confidence

    _normalise = field_validator("stated_rationale", "who_stated_it", mode="after")(
        lambda cls, value: _collapse_abstention(value)
    )

    @property
    def speaker(self) -> str:
        """The pipeline reads `speaker` uniformly. For a decision it is whoever
        stated it, which is the same person who spoke the quote."""
        return self.who_stated_it


class DraftDecisionList(StrictModel):
    decisions: list[DraftDecision] = Field(default_factory=list)


class DraftRisk(StrictModel):
    """One risk or blocker as the model proposes it (M5).

    Severity is a plain enum rather than a number, because the capability test
    asks whether it is defensible from the quote alone, and a reviewer can
    argue with three named bands in a way they cannot argue with 0.72.
    """

    description: str
    severity: Severity
    affected_area: str = Field(description="the work at stake, or UNSPECIFIED")
    owner: str = Field(description="who is accountable, or UNSPECIFIED. Raising is not owning.")
    verbatim_quote: str
    speaker: str
    timestamp: str
    confidence: Confidence

    _normalise = field_validator("affected_area", "owner", mode="after")(
        lambda cls, value: _collapse_abstention(value)
    )


class DraftRiskList(StrictModel):
    risks: list[DraftRisk] = Field(default_factory=list)


class DraftSignal(StrictModel):
    """One classified chat message as the model proposes it (M9).

    `message_id` is copied back rather than inferred from position, because a
    model that drops or reorders an entry would otherwise shift every label
    onto the wrong message and the mistake would be invisible.
    """

    message_id: str
    classification: SignalClass
    quote: str = Field(description="copied character for character from that message")
    reason: str = Field(description="why this label, under 15 words")
    confidence: Confidence


class DraftSignalList(StrictModel):
    signals: list[DraftSignal] = Field(default_factory=list)


# =============================================================================
# What the store holds
# =============================================================================


class ResolvedDate(StrictModel):
    """A due date, plus how it came to have its value.

    `value` is what downstream consumers read: an ISO date, or UNSPECIFIED.

    `stated_text` and `date_type` exist because golden case 4 measures invented
    dates, and a resolved date is otherwise indistinguishable from one the
    transcript stated. Without provenance the metric cannot be computed at all.
    `rule` names the rule applied, which the brief requires be documented.
    """

    value: str = UNSPECIFIED
    date_type: DateType = DateType.UNSPECIFIED
    stated_text: str | None = None
    rule: str | None = None

    @property
    def is_concrete(self) -> bool:
        return self.value != UNSPECIFIED


class QuoteLocation(StrictModel):
    """Where a verified quote sits in the source text.

    Present only when verification succeeded, because an unverified quote by
    definition has no location.
    """

    char_start: int
    char_end: int
    segment_id: str | None = None


class ActionItem(StrictModel):
    """A stored action item (M3)."""

    what: str
    owner: str = UNSPECIFIED
    due_date: ResolvedDate = Field(default_factory=ResolvedDate)


class Decision(StrictModel):
    """A stored decision (M4). Populated in Phase 5."""

    what_was_decided: str
    stated_rationale: str = UNSPECIFIED
    who_stated_it: str = UNSPECIFIED
    alternatives_discussed: list[str] = Field(default_factory=list)


class Risk(StrictModel):
    """A stored risk or blocker (M5). Populated in Phase 5."""

    description: str
    severity: Severity
    affected_area: str = UNSPECIFIED
    owner: str = UNSPECIFIED


class Extraction(StrictModel):
    """One proposal, in whatever state review has left it.

    `payload` is the current, possibly human-edited body. `original_payload`
    is what the model first returned and is immutable, enforced by trigger, so
    the review surface can always show one beside the other.
    """

    id: str
    source_id: str
    extraction_type: ExtractionType

    payload: dict
    original_payload: dict

    verbatim_quote: str
    quote_verified: bool = False
    quote_location: QuoteLocation | None = None

    speaker: str | None = None
    timestamp: str | None = None
    segment_id: str | None = None
    message_id: str | None = None

    confidence: Confidence | None = None
    dedup_key: str | None = None
    chunk_id: str | None = None
    merged_from: list[str] = Field(default_factory=list)

    provider: str | None = None
    model_name: str | None = None
    prompt_version: str | None = None

    status: ReviewStatus = ReviewStatus.PENDING
    reviewer: str | None = None
    reviewed_at: str | None = None
    review_note: str | None = None
    expires_at: str | None = None
    created_at: str

    @property
    def needs_override_to_approve(self) -> bool:
        """An unverified quote must not slip through on a distracted click."""
        return not self.quote_verified
