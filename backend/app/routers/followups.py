"""HTTP surface for M12.

There is no approval gate on drafting. Every line in a draft came from an
extraction a human already approved, and a second gate would ask a reviewer to
approve their own earlier approvals.

There is a gate on sending, and it is the only one of its kind in this build.
Every send names the person sending it. The endpoint has no default for that
field, no service account, and no scheduled counterpart, because the capability
says the agent never sends.
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Query

from app.config import get_settings
from app.followup import draft as service
from app.models.common import StrictModel
from app.models.followup import FollowUpDraft

router = APIRouter(prefix="/api/followups", tags=["follow-up drafts"])


class EditRequest(StrictModel):
    body: str
    edited_by: str


class SendRequest(StrictModel):
    #: Required, and refused when blank or when it names a service. There is
    #: deliberately no default: a default here would be the agent sending.
    sent_by: str
    channel: str = "recap"


@router.get("", response_model=list[FollowUpDraft], summary="Drafts already created")
def list_drafts(source_id: str | None = Query(default=None)) -> list[FollowUpDraft]:
    return service.list_drafts(source_id, get_settings())


@router.get("/preview/{source_id}", response_model=FollowUpDraft,
            summary="Build a recap without storing it")
def preview(source_id: str) -> FollowUpDraft:
    """Returns the lines as well as the text, so the interface can show each
    one against the quote it came from rather than parsing the markdown back."""
    return service.build_draft(source_id, get_settings())


@router.post("/{source_id}", response_model=FollowUpDraft, summary="Create a draft")
def create(source_id: str) -> FollowUpDraft:
    """A new version every time. A recap drafted before three more items were
    approved is a different message, and the earlier one may already be sent."""
    return service.create_draft(source_id, get_settings())


@router.get("/{draft_id}", response_model=FollowUpDraft, summary="Read one draft")
def get(draft_id: str) -> FollowUpDraft:
    return service.get_draft(draft_id, get_settings())


@router.put("/{draft_id}", response_model=FollowUpDraft, summary="Store the human's version")
def edit(draft_id: str, request: EditRequest = Body(...)) -> FollowUpDraft:
    """The generated text is not replaced. Both versions stay readable, so what
    the person changed is answerable at any point afterwards."""
    return service.edit_draft(draft_id, request.body, request.edited_by, get_settings())


@router.post("/{draft_id}/send", response_model=FollowUpDraft, summary="A person sends it")
def send(draft_id: str, request: SendRequest = Body(...)) -> FollowUpDraft:
    """403 when sent_by is blank or names a service.

    Refused here for a readable message, and refused again by
    trg_followup_send_requires_person and trg_followup_agent_cannot_send if
    this endpoint is bypassed.
    """
    return service.send_draft(draft_id, request.sent_by, request.channel, get_settings())
