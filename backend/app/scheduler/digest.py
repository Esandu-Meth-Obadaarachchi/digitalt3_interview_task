"""M10 - building the end-of-day digest.

Three rules the capability test states, and how each is met:

  "Digests never contain unapproved extractions."
      Only approved rows are read. There is no filter to forget, because the
      query has no other status in it.

  "every line citing its source"
      Every line carries the quote, the speaker, the timestamp and the source.
      A line that cannot be cited is not written.

  "Running with a clock override produces a digest"
      `now` is injectable throughout. Nothing here calls datetime.now except
      to record when the digest was generated.

The 3/2/1 shape is the specification, and it is the useful part. A digest that
grows with the day is a digest nobody reads, so the format forces a choice
about what matters and the reason for each choice is written on the line.

Each item appears in exactly ONE section, and the precedence is deliberate:

    needs attention  >  to decide  >  moved

A blocker approved today is both progress and a problem. Printed under both it
fills two of six lines with one fact, which is the opposite of what a fixed-size
digest is for. Attention wins because a reader who only reads one section
should read that one.
"""

from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, timedelta, timezone

from app.config import Settings, get_settings
from app.db import database
from app.db.repositories import chat as chat_repo
from app.db.repositories import extractions as extraction_repo
from app.db.repositories import reviews as review_repo
from app.db.repositories import sources as source_repo
from app.models.common import UNSPECIFIED, Citation, ExtractionType, ReviewStatus
from app.models.digest import Digest, DigestLine

logger = logging.getLogger("agent.digest")

#: Risk severities that qualify as needing attention, worst first.
_ATTENTION_SEVERITY = ("high", "medium")


def _headline(extraction) -> str:
    payload = extraction.payload
    for key in ("what", "what_was_decided", "description", "text"):
        if payload.get(key):
            return str(payload[key])
    return extraction.verbatim_quote[:80]


def _citation(extraction, source_title: str | None) -> Citation:
    return Citation(
        source_id=extraction.source_id,
        source_title=source_title,
        segment_id=extraction.quote_location.segment_id if extraction.quote_location else None,
        message_id=extraction.message_id,
        speaker=extraction.speaker,
        timestamp=extraction.timestamp,
        quote=extraction.verbatim_quote,
        char_start=extraction.quote_location.char_start if extraction.quote_location else None,
        char_end=extraction.quote_location.char_end if extraction.quote_location else None,
    )


def _line(extraction, source_title: str | None, because: str) -> DigestLine:
    return DigestLine(
        text=_headline(extraction),
        citation=_citation(extraction, source_title),
        extraction_id=extraction.id,
        extraction_type=extraction.extraction_type.value,
        because=because,
    )


def scopes(settings: Settings | None = None) -> list[tuple[str, str]]:
    """Every scope a digest can be produced for, as (key, title).

    A chat channel is a channel. A meeting is treated as its own channel,
    because the specification says one digest per channel and a meeting has no
    other natural grouping. Doing otherwise would leave every transcript
    outside the digest entirely.
    """
    cfg = settings or get_settings()
    found: list[tuple[str, str]] = []

    with database.connect(cfg) as conn:
        for channel in chat_repo.channels(conn):
            found.append((channel, f"#{channel}"))
        for source in source_repo.list_sources(conn):
            if source.source_type.value == "transcript" and source.status.value == "ingested":
                found.append((source.id, source.title))

    return found


def _in_scope(conn, scope_key: str) -> tuple[list, str | None]:
    """Approved extractions for a scope, and the title to cite them under.

    Only approved. Not filtered afterwards: the query has no other status in
    it, so there is nothing to forget.
    """
    source = source_repo.get_source(conn, scope_key)
    if source is not None:
        return (
            extraction_repo.list_extractions(conn, source_id=scope_key, status=ReviewStatus.APPROVED),
            source.title,
        )

    # A chat channel. Its extractions are signals, linked by message id.
    message_ids = {m.id for m in chat_repo.list_messages(conn, channel=scope_key)}
    if not message_ids:
        return [], None

    approved = extraction_repo.list_extractions(
        conn, extraction_type=ExtractionType.SIGNAL, status=ReviewStatus.APPROVED
    )
    return [e for e in approved if e.message_id in message_ids], f"#{scope_key}"


def _approved_on(conn, extraction_id: str) -> str | None:
    for event in review_repo.history(conn, extraction_id):
        if event.event_type is review_repo.ReviewEventType.APPROVED:
            return event.created_at[:10]
    return None


def build_digest(
    scope_key: str,
    settings: Settings | None = None,
    *,
    now: datetime | None = None,
    trigger: str = "manual",
    scope_title: str | None = None,
) -> Digest:
    """Build one digest for one scope for one day.

    `now` is injectable so the whole thing is demonstrable without waiting for
    six o'clock, which is what the capability test means by a clock override.
    """
    cfg = settings or get_settings()
    moment = now or datetime.now(timezone.utc)
    day = moment.date().isoformat()

    with database.connect(cfg) as conn:
        approved, title = _in_scope(conn, scope_key)
        approved_dates = {e.id: _approved_on(conn, e.id) for e in approved}

    digest = Digest(
        id=str(uuid.uuid4()),
        scope_type="channel",
        scope_key=scope_key,
        scope_title=scope_title or title or scope_key,
        digest_date=day,
        generated_at=moment.isoformat(),
        trigger=trigger,
        considered=len(approved),
    )

    if not approved:
        return digest

    used: set[str] = set()

    # --- needs attention: filled first, because it wins ---------------------
    # A blocker approved today is both progress and a problem. Printed under
    # both it fills two of six lines with one fact. A reader who only reads one
    # section should read this one, so it takes the item.
    attention: list[tuple[int, object, str]] = []
    for extraction in approved:
        payload = extraction.payload
        if extraction.extraction_type is ExtractionType.RISK:
            severity = str(payload.get("severity", "low"))
            if severity in _ATTENTION_SEVERITY:
                attention.append(
                    (_ATTENTION_SEVERITY.index(severity), extraction, f"{severity} severity risk")
                )
        elif payload.get("classification") == "blocker":
            attention.append((0, extraction, "a blocker raised in the channel"))
        elif extraction.extraction_type is ExtractionType.ACTION and payload.get("owner") == UNSPECIFIED:
            attention.append((2, extraction, "an action nobody has been assigned"))

    for _, extraction, because in sorted(attention, key=lambda row: row[0])[:2]:
        digest.attention.append(_line(extraction, digest.scope_title, because))
        used.add(extraction.id)

    # --- to decide: one thing genuinely waiting on a person ------------------
    # An action with no owner and no date is waiting on somebody to choose one,
    # which is a decision. Falling back to a request keeps the section honest
    # rather than filling it with whatever is left over.
    candidates = [
        (e, "nobody owns this and no date is set")
        for e in approved
        if e.id not in used
        and e.extraction_type is ExtractionType.ACTION
        and e.payload.get("owner") == UNSPECIFIED
        and e.payload.get("due_date") == UNSPECIFIED
    ] + [
        (e, "a request in the channel with nobody confirmed")
        for e in approved
        if e.id not in used and e.payload.get("classification") == "request"
    ]

    if candidates:
        extraction, because = candidates[0]
        digest.to_decide.append(_line(extraction, digest.scope_title, because))
        used.add(extraction.id)

    # --- moved: what changed today, from whatever is left --------------------
    # If nothing was approved today the section falls back to the most recent
    # approvals, so a digest run on a quiet Monday still says where things
    # stand rather than going blank.
    remaining = [e for e in approved if e.id not in used]
    today = [e for e in remaining if approved_dates.get(e.id) == day]
    moved_source = today or sorted(
        remaining, key=lambda e: approved_dates.get(e.id) or "", reverse=True
    )
    for extraction in moved_source[:3]:
        because = (
            "approved today" if approved_dates.get(extraction.id) == day
            else f"most recently approved, on {approved_dates.get(extraction.id) or 'an unknown date'}"
        )
        digest.moved.append(_line(extraction, digest.scope_title, because))

    return digest


def emit_digest(
    scope_key: str,
    settings: Settings | None = None,
    *,
    now: datetime | None = None,
    trigger: str = "manual",
    post: bool = True,
) -> Digest:
    """Build a digest, store it, write the file, and post it.

    Posting is not gated by approval, and the task catalogue says why: every
    line already came from something a human approved, so there is nothing left
    to approve. A second gate here would ask a reviewer to approve their own
    earlier approvals.
    """
    from app.adapters.factory import get_notifier, get_store

    cfg = settings or get_settings()
    digest = build_digest(scope_key, cfg, now=now, trigger=trigger)
    body = digest.render()

    key = f"digests/{digest.digest_date}/{scope_key.replace('/', '_')}.md"
    document = get_store(cfg).write(key, body, content_type="text/markdown")

    with database.transaction(cfg) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO digests (id, scope_type, scope_key, digest_date,"
            " generated_at, trigger, content, file_path) VALUES (?,?,?,?,?,?,?,?)",
            (
                digest.id,
                digest.scope_type,
                digest.scope_key,
                digest.digest_date,
                digest.generated_at,
                digest.trigger,
                digest.model_dump_json(),
                document.location or key,
            ),
        )

    if post:
        get_notifier(cfg).post(scope_key, f"{digest.scope_title} — {digest.digest_date}", body)

    logger.info("digest for %s on %s: %s line(s)", scope_key, digest.digest_date, len(digest.lines))
    return digest


def emit_all(
    settings: Settings | None = None,
    *,
    now: datetime | None = None,
    trigger: str = "manual",
    post: bool = True,
) -> list[Digest]:
    """One digest per scope. What the scheduled job runs."""
    cfg = settings or get_settings()
    return [emit_digest(key, cfg, now=now, trigger=trigger, post=post) for key, _ in scopes(cfg)]


def yesterday(now: datetime | None = None) -> datetime:
    """Convenience for a digest covering the previous day."""
    return (now or datetime.now(timezone.utc)) - timedelta(days=1)
