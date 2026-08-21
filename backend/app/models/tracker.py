"""Contracts for the tracker adapter (M7).

Nothing here names a specific product. `TrackerItemDraft` is what the agent
asks any tracker to create, and `TrackerItem` is what any tracker gives back.
A Jira client and the mock both speak this vocabulary, which is the property
the adapter contract is assessed on: the mock's data shape must not have leaked
into the agent's logic.

`source_ref` is the one field that points back at us. It carries the extraction
id, which a real integration would put in a custom field or at the end of the
description. It is how a re-run recognises what it already wrote, and how a
person reading a ticket can find the sentence somebody actually said.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import ConfigDict, Field

from app.models.common import StrictModel


class WriteOutcome(StrEnum):
    CREATED = "created"
    DEDUPLICATED = "deduplicated"   # already written, no second item made
    BLOCKED = "blocked"             # the approval gate refused it


class TrackerItemDraft(StrictModel):
    """What the agent asks a tracker to create.

    Deliberately not "an issue" or "a ticket" or "a work item": whatever the
    foreign system calls it, this is the shape the agent knows how to describe.
    """

    title: str
    description: str
    assignee: str | None = None
    due_date: str | None = None
    labels: list[str] = Field(default_factory=list)
    source_ref: str | None = None


class TrackerItem(StrictModel):
    """What a tracker gives back.

    `status` is a free-text string rather than an enum on purpose. Real
    trackers let teams invent their own workflow states, and an agent that
    assumes a fixed set breaks on contact with a real backlog.

    Whitespace is NOT stripped here, unlike every other contract in this
    codebase. This model represents somebody else's data, and quietly
    normalising "In Progress " into "In Progress" would hide exactly the mess
    the adapter contract requires the agent to cope with. Our own contracts
    strip; foreign data is preserved as it was found.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)

    external_ref: str
    title: str
    description: str | None = None
    assignee: str | None = None
    status: str = "open"
    due_date: str | None = None
    labels: list[str] = Field(default_factory=list)
    source_ref: str | None = None
    created_at: str | None = None
    updated_at: str | None = None

    @property
    def written_by_agent(self) -> bool:
        return self.source_ref is not None


class TrackerFilter(StrictModel):
    """The narrow slice of filtering the agent actually needs."""

    status: str | None = None
    assignee: str | None = None
    source_ref: str | None = None
    written_by_agent: bool | None = None
    limit: int | None = None


class WriteAttempt(StrictModel):
    """One attempt to write, including the refused ones.

    tracker_writes answers "what exists in the tracker". This answers "what did
    the agent try to do", which is what proves deduplication happened and what
    proves the gate fired.
    """

    id: str
    extraction_id: str
    outcome: WriteOutcome
    provider: str
    attempted_at: str
    external_ref: str | None = None
    reason: str | None = None


class WriteResult(StrictModel):
    """The result of asking the tracker to record an approved action."""

    outcome: WriteOutcome
    extraction_id: str
    item: TrackerItem | None = None
    reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.outcome is not WriteOutcome.BLOCKED
