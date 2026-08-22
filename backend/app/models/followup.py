"""M12 - the follow-up message draft.

    "Generate a recap email/message from approved items. Human edits and
     sends. Agent never sends."

Every clause of that is about who acts, so the model keeps the two authors
apart. `generated_body` is what the system wrote and never changes. `edited_body`
is what the person wrote instead. `sent_by` is the person who sent it, and it is
never a service: the database refuses a send naming one.

The draft carries no new claims. Every line is built from an approved
extraction and quotes the same verbatim text the reviewer saw, so a recap
cannot say something the transcript does not.
"""

from __future__ import annotations

from pydantic import Field

from app.models.common import UNSPECIFIED, Citation, StrictModel


class FollowUpLine(StrictModel):
    """One line of the recap, and the approved item behind it."""

    text: str
    citation: Citation
    extraction_id: str
    extraction_type: str
    #: Which part of the recap it belongs to: commitments, decisions, risks.
    section: str
    owner: str = UNSPECIFIED
    due_date: str = UNSPECIFIED


class FollowUpDraft(StrictModel):
    """A recap message waiting for a person to edit and send."""

    id: str
    source_id: str
    source_title: str | None = None
    draft_version: int

    subject: str
    #: What the system produced. Immutable, enforced by trigger.
    generated_body: str
    #: What the person wrote instead. None until they touch it.
    edited_body: str | None = None
    edited_by: str | None = None
    edited_at: str | None = None

    status: str = "draft"          # draft | edited | sent
    item_count: int = 0
    generated_at: str

    channel: str | None = None
    sent_by: str | None = None     # a person, never a service
    sent_at: str | None = None
    notification_id: str | None = None

    #: Present when the draft was just built, so the interface can show each
    #: line against its citation rather than parsing the markdown back.
    lines: list[FollowUpLine] = Field(default_factory=list)

    @property
    def body(self) -> str:
        """What would actually be sent: the human's version if there is one."""
        return self.edited_body if self.edited_body is not None else self.generated_body

    @property
    def human_edited(self) -> bool:
        return self.edited_body is not None and self.edited_body != self.generated_body

    @property
    def sent(self) -> bool:
        return self.status == "sent"
