"""The tracker interface (M7).

The adapter contract states the test that will be applied:

    "could a real integration be dropped in by writing one new class and
     changing one line of wiring, with zero changes to agent logic?"

So this interface names four operations, and every one of them has a caller in
this repository. `add_comment` appears in the brief's illustrative example and
is deliberately absent here, because no capability in this build comments on a
ticket, and the same contract warns that a capability implied but not exercised
reads as padding.

Nothing in this file knows what a tracker is made of. `TrackerItemDraft` is
what the agent asks any tracker to create; `TrackerItem` is what any tracker
gives back. Neither mentions SQLite, JSONL, Jira or an extraction. The one link
back to us is `source_ref`, which a real integration would put in a custom
field or at the end of the description.

The approval gate is not enforced here, on purpose. It is enforced in the
tracker service above this interface, and by a database trigger below it.
Putting it in the adapter would mean every future adapter had to reimplement
it, and the one that forgot would be the one that mattered.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.models.tracker import TrackerFilter, TrackerItem, TrackerItemDraft


class TrackerAdapter(ABC):
    """A work tracker, reduced to what this agent actually does with one."""

    #: Recorded on every write so an audit row says which implementation wrote it.
    provider: str

    @abstractmethod
    def create_item(self, draft: TrackerItemDraft) -> TrackerItem:
        """Create one item and return it as the tracker now holds it."""

    @abstractmethod
    def get_item(self, external_ref: str) -> TrackerItem | None:
        """Fetch one item, or None when the tracker has no such reference."""

    @abstractmethod
    def list_items(self, criteria: TrackerFilter | None = None) -> list[TrackerItem]:
        """List items, including ones this agent did not create."""

    @abstractmethod
    def transition(self, external_ref: str, status: str) -> TrackerItem:
        """Move an item to a new status.

        `status` is free text because real trackers let teams invent their own
        workflow states, and an agent that assumes a fixed set breaks on
        contact with a real backlog.
        """

    def describe(self) -> str:
        return self.provider
