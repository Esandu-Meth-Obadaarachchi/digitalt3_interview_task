"""M12 - the recap message, drafted for a person to edit and send.

    "Generate a recap email/message from approved items. Human edits and
     sends. Agent never sends."

Every clause is about who acts, so the module is organised around that rather
than around text generation.

  Drafting is not an external write. Nothing leaves the machine, and every line
  came from something a human already approved. There is no second approval
  gate here, for the same reason the digest has none: it would ask a reviewer
  to approve their own earlier approvals.

  Sending is an external write, and it is the only thing in this build a person
  triggers by hand every single time. No scheduler job sends. No code path
  sends without a named person, and the refusal is written three times over:
  here, by trg_followup_send_requires_person, and by
  trg_followup_agent_cannot_send.

The recap is rendered from the approved rows rather than written by the model.
That is the more interesting decision and the reason is the same one the whole
build turns on: a model asked to summarise approved items produces sentences
nobody approved. A template produces exactly the approved text with its quote
attached, and a recap is one of the few documents where saying it plainly is
better than saying it well.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from app.config import Settings, get_settings
from app.db import database
from app.db.repositories import extractions as extraction_repo
from app.db.repositories import sources as source_repo
from app.errors import AgentSendRefused, NotFoundError, ReviewStateError
from app.models.common import UNSPECIFIED, ExtractionType, ReviewStatus
from app.models.followup import FollowUpDraft, FollowUpLine
from app.scheduler.digest import _citation, _headline

logger = logging.getLogger("agent.followup")

#: Names that are a service rather than a person. Kept beside the identical
#: SQL list so the readable refusal and the enforced one say the same thing.
SERVICE_NAMES = {"agent", "system", "scheduler", "bot", "service", "llm", "model"}

_SECTIONS = (
    (ExtractionType.ACTION, "commitments", "What people committed to"),
    (ExtractionType.DECISION, "decisions", "What was decided"),
    (ExtractionType.RISK, "risks", "Risks and blockers raised"),
)


def _approved(conn, source_id: str) -> list:
    """Approved extractions for one source. No other status is queried."""
    return extraction_repo.list_extractions(
        conn, source_id=source_id, status=ReviewStatus.APPROVED
    )


def _render(title: str, meeting_date: str | None, lines: list[FollowUpLine]) -> str:
    """The recap as a person would send it.

    Plain, cited, and dull on purpose. Every line carries the quote it came
    from, so a recipient who disagrees with a line can check it without asking
    anybody for the recording.
    """
    out = [f"# Recap: {title}", ""]
    if meeting_date:
        out += [f"_Meeting held {meeting_date}. Drafted from approved items only._", ""]
    else:
        out += ["_Drafted from approved items only._", ""]

    for _, section, heading in _SECTIONS:
        chosen = [line for line in lines if line.section == section]
        if not chosen:
            continue
        out += [f"## {heading}", ""]
        for line in chosen:
            out.append(f"- {line.text}")
            if section == "commitments":
                owner = (
                    "assignee UNSPECIFIED, nobody was named"
                    if line.owner == UNSPECIFIED or not line.owner
                    else line.owner
                )
                due = (
                    "no date stated" if line.due_date in (UNSPECIFIED, "", None)
                    else f"due {line.due_date}"
                )
                out.append(f"  {owner} · {due}")
            citation = line.citation
            when = f" at {citation.timestamp}" if citation.timestamp else ""
            who = citation.speaker or "unattributed"
            out.append(f'  > "{citation.quote}"')
            out.append(f"  — {who}{when}")
        out.append("")

    out += [
        "---",
        "",
        f"_Built from {len(lines)} approved item(s). Nothing unapproved appears above. "
        f"This is a draft: a person edits it and a person sends it._",
    ]
    return "\n".join(out)


def build_draft(
    source_id: str,
    settings: Settings | None = None,
    *,
    now: datetime | None = None,
) -> FollowUpDraft:
    """Build the recap for one source without storing anything.

    Raises NotFoundError when the source has no approved items. A recap of
    nothing is not a recap, and an empty one sent by mistake says the meeting
    produced nothing.
    """
    cfg = settings or get_settings()
    moment = now or datetime.now(timezone.utc)

    with database.connect(cfg) as conn:
        source = source_repo.get_source(conn, source_id)
        if source is None:
            raise NotFoundError(f"no source {source_id}")
        approved = _approved(conn, source_id)

    if not approved:
        raise NotFoundError(
            f"nothing approved for {source_id}. A recap is built from approved items, "
            f"so there is nothing to draft yet."
        )

    lines: list[FollowUpLine] = []
    for kind, section, _ in _SECTIONS:
        for extraction in [e for e in approved if e.extraction_type is kind]:
            payload = extraction.payload
            lines.append(
                FollowUpLine(
                    text=_headline(extraction),
                    citation=_citation(extraction, source.title),
                    extraction_id=extraction.id,
                    extraction_type=kind.value,
                    section=section,
                    owner=str(payload.get("owner", UNSPECIFIED)) or UNSPECIFIED,
                    due_date=str(payload.get("due_date", UNSPECIFIED)) or UNSPECIFIED,
                )
            )

    body = _render(source.title, source.meeting_date, lines)
    return FollowUpDraft(
        id=str(uuid.uuid4()),
        source_id=source_id,
        source_title=source.title,
        draft_version=0,          # assigned when stored
        subject=f"Recap: {source.title}",
        generated_body=body,
        status="draft",
        item_count=len(lines),
        generated_at=moment.isoformat(),
        lines=lines,
    )


def create_draft(
    source_id: str,
    settings: Settings | None = None,
    *,
    now: datetime | None = None,
) -> FollowUpDraft:
    """Build a recap and store it as a new version.

    Versions accumulate rather than replace. A recap drafted before three more
    items were approved is a different message, and the earlier one may already
    have been sent.
    """
    cfg = settings or get_settings()
    draft = build_draft(source_id, cfg, now=now)

    with database.transaction(cfg) as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(draft_version), 0) AS v FROM followup_drafts WHERE source_id = ?",
            (source_id,),
        ).fetchone()
        version = int(row["v"]) + 1
        conn.execute(
            "INSERT INTO followup_drafts (id, source_id, draft_version, subject,"
            " generated_body, status, item_count, generated_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                draft.id, source_id, version, draft.subject,
                draft.generated_body, "draft", draft.item_count, draft.generated_at,
            ),
        )

    logger.info("follow-up draft %s v%s for %s, %s item(s)",
                draft.id, version, source_id, draft.item_count)
    return draft.model_copy(update={"draft_version": version})


def _row_to_draft(row) -> FollowUpDraft:
    return FollowUpDraft(
        id=row["id"],
        source_id=row["source_id"],
        source_title=row["source_title"] if "source_title" in row.keys() else None,
        draft_version=row["draft_version"],
        subject=row["subject"],
        generated_body=row["generated_body"],
        edited_body=row["edited_body"],
        edited_by=row["edited_by"],
        edited_at=row["edited_at"],
        status=row["status"],
        item_count=row["item_count"],
        generated_at=row["generated_at"],
        channel=row["channel"],
        sent_by=row["sent_by"],
        sent_at=row["sent_at"],
        notification_id=row["notification_id"],
    )


def get_draft(draft_id: str, settings: Settings | None = None) -> FollowUpDraft:
    cfg = settings or get_settings()
    with database.connect(cfg) as conn:
        row = conn.execute(
            "SELECT d.*, s.title AS source_title FROM followup_drafts d"
            " LEFT JOIN sources s ON s.id = d.source_id WHERE d.id = ?",
            (draft_id,),
        ).fetchone()
    if row is None:
        raise NotFoundError(f"no follow-up draft {draft_id}")
    return _row_to_draft(row)


def list_drafts(
    source_id: str | None = None, settings: Settings | None = None
) -> list[FollowUpDraft]:
    cfg = settings or get_settings()
    clause = " WHERE d.source_id = ?" if source_id else ""
    params = (source_id,) if source_id else ()
    with database.connect(cfg) as conn:
        rows = conn.execute(
            "SELECT d.*, s.title AS source_title FROM followup_drafts d"
            " LEFT JOIN sources s ON s.id = d.source_id"
            f"{clause} ORDER BY d.generated_at DESC, d.draft_version DESC",
            params,
        ).fetchall()
    return [_row_to_draft(row) for row in rows]


def edit_draft(
    draft_id: str,
    body: str,
    edited_by: str,
    settings: Settings | None = None,
    *,
    now: datetime | None = None,
) -> FollowUpDraft:
    """Store what the person wrote instead.

    The generated text is untouched. Which half a reader is looking at is the
    first question worth answering about a machine-drafted message, and the
    trigger on generated_body makes the answer unforgeable.
    """
    cfg = settings or get_settings()
    if not edited_by or not edited_by.strip():
        raise AgentSendRefused("an edit records the person who made it")

    draft = get_draft(draft_id, cfg)
    if draft.sent:
        raise ReviewStateError("this draft was already sent and cannot be edited")

    moment = (now or datetime.now(timezone.utc)).isoformat()
    with database.transaction(cfg) as conn:
        conn.execute(
            "UPDATE followup_drafts SET edited_body = ?, edited_by = ?, edited_at = ?,"
            " status = 'edited' WHERE id = ?",
            (body, edited_by.strip(), moment, draft_id),
        )
    return get_draft(draft_id, cfg)


def send_draft(
    draft_id: str,
    sent_by: str,
    channel: str,
    settings: Settings | None = None,
    *,
    now: datetime | None = None,
) -> FollowUpDraft:
    """Send what the person has in front of them, as that person.

    `sent_by` is not a courtesy field. It is the difference between a person
    sending a message and an agent sending one, so a blank value or a service
    name is refused here, and refused again by two triggers if this check is
    bypassed.
    """
    from app.adapters.factory import get_notifier

    cfg = settings or get_settings()
    named = (sent_by or "").strip()
    if not named:
        raise AgentSendRefused(
            "sending requires the person sending it. The agent never sends."
        )
    if named.casefold() in SERVICE_NAMES:
        raise AgentSendRefused(
            f"'{named}' is a service, not a person. The agent never sends."
        )

    draft = get_draft(draft_id, cfg)
    if draft.sent:
        raise ReviewStateError(f"draft {draft_id} was already sent by {draft.sent_by}")

    moment = (now or datetime.now(timezone.utc)).isoformat()
    notification = get_notifier(cfg).post(channel, draft.subject, draft.body)

    with database.transaction(cfg) as conn:
        conn.execute(
            "UPDATE followup_drafts SET status = 'sent', sent_by = ?, sent_at = ?,"
            " channel = ?, notification_id = ? WHERE id = ?",
            (named, moment, channel, notification.id, draft_id),
        )

    logger.info("follow-up %s sent by %s to %s", draft_id, named, channel)
    return get_draft(draft_id, cfg)
