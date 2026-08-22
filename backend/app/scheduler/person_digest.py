"""M13 - the per-person digest.

    "Per-person view of their commitments. Person with no commitments gets no
     digest."

Three things separate this from the channel digest, and each is in the
specification rather than a preference.

  It is cross-source. A commitment is a commitment whichever meeting it was
  made in, and a person opening their digest wants all of them. The channel
  digest is per source because a channel is a place; a person is not.

  It has no fixed size. The 3/2/1 shape exists to force a choice about what
  matters across a whole channel. Capping a person's own commitments would drop
  the fourth one silently, which is the opposite of the point.

  Somebody with nothing approved gets no digest at all. Not an empty file, not
  a cheerful nothing-to-do note. `emit_all_people` skips them, so the absence is
  the behaviour rather than a rendering detail.

Unowned work is not dropped. It goes to one digest of its own where every line
states the task and says the assignee is unspecified, which is the only honest
place for it: dropping it hides the work, and assigning it invents an owner.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from app.config import Settings, get_settings
from app.db import database
from app.db.repositories import extractions as extraction_repo
from app.db.repositories import sources as source_repo
from app.models.common import UNSPECIFIED, ExtractionType, ReviewStatus
from app.models.digest import PersonDigest, PersonDigestLine
from app.people.identity import UNASSIGNED, Person, group_owners, person_key
# The citation and approval-date helpers are the channel digest's, used
# unchanged. A person digest cites exactly what a channel digest cites, and two
# implementations of one citation would be two things to keep honest.
from app.scheduler.digest import _approved_on, _citation, _headline

logger = logging.getLogger("agent.digest.person")


def _approved_actions(conn) -> list:
    """Every approved action, across every source.

    Only approved. The query has no other status in it, so there is no filter
    to forget later.
    """
    return extraction_repo.list_extractions(
        conn, extraction_type=ExtractionType.ACTION, status=ReviewStatus.APPROVED
    )


def _participants(conn) -> list[str]:
    """Names the meetings themselves supplied, used to expand a first name."""
    names: list[str] = []
    for source in source_repo.list_sources(conn):
        names.extend(source.participants or [])
    return names


def people(settings: Settings | None = None) -> list[Person]:
    """Every person a digest can be produced for, unassigned work last."""
    cfg = settings or get_settings()
    with database.connect(cfg) as conn:
        actions = _approved_actions(conn)
        known = _participants(conn)
    owners = [str(a.payload.get("owner", UNSPECIFIED)) for a in actions]
    return group_owners(owners, known, identity=cfg.person_identity)


def _person(key: str, settings: Settings) -> Person | None:
    for candidate in people(settings):
        if candidate.key == key:
            return candidate
    return None


def build_person_digest(
    key: str,
    settings: Settings | None = None,
    *,
    now: datetime | None = None,
    trigger: str = "manual",
) -> PersonDigest:
    """One person's approved commitments, for one day.

    `now` is injectable for the same reason as everywhere else: the whole thing
    has to be demonstrable without waiting for six o'clock.
    """
    cfg = settings or get_settings()
    moment = now or datetime.now(timezone.utc)
    matched = _person(key, cfg)

    with database.connect(cfg) as conn:
        actions = _approved_actions(conn)
        titles = {s.id: s.title for s in source_repo.list_sources(conn)}
        mine = [
            a for a in actions
            if person_key(str(a.payload.get("owner", UNSPECIFIED)), cfg.person_identity) == key
        ]
        approved_dates = {a.id: _approved_on(conn, a.id) for a in mine}

    digest = PersonDigest(
        id=str(uuid.uuid4()),
        person_key=key,
        display_name=(matched.display_name if matched else key),
        aliases=(matched.aliases if matched else []),
        unassigned=key == UNASSIGNED,
        digest_date=moment.date().isoformat(),
        generated_at=moment.isoformat(),
        trigger=trigger,
        considered=len(mine),
    )

    # Dated commitments first, and within each group the earliest approval
    # first, so the order is the order things were agreed rather than the order
    # the database returned them.
    for action in sorted(mine, key=lambda a: (approved_dates.get(a.id) or "", a.id)):
        payload = action.payload
        digest.commitments.append(
            PersonDigestLine(
                text=_headline(action),
                citation=_citation(action, titles.get(action.source_id)),
                extraction_id=action.id,
                owner_as_stated=str(payload.get("owner", UNSPECIFIED)) or UNSPECIFIED,
                due_date=str(payload.get("due_date", UNSPECIFIED)) or UNSPECIFIED,
                approved_on=approved_dates.get(action.id),
            )
        )

    return digest


def emit_person_digest(
    key: str,
    settings: Settings | None = None,
    *,
    now: datetime | None = None,
    trigger: str = "manual",
    post: bool | None = None,
) -> PersonDigest | None:
    """Build one person digest, store it, write the file.

    Returns None for a person with no approved commitments, and writes nothing.
    That is M13's own rule and it is enforced here rather than by rendering an
    empty file somebody has to read to discover it is empty.

    Posting is off by default. A channel digest is written for a channel, but a
    person's workload posted into a shared channel shows everyone what one
    person is carrying, which is a different thing from the digest they asked
    for. POST_PERSON_DIGESTS turns it on for a demonstration.
    """
    from app.adapters.factory import get_notifier, get_store

    cfg = settings or get_settings()
    digest = build_person_digest(key, cfg, now=now, trigger=trigger)
    if digest.empty:
        logger.info("no digest for %s: nothing approved stands against this name", key)
        return None

    body = digest.render()
    file_key = f"digests/{digest.digest_date}/person-{key.replace('/', '_')}.md"
    document = get_store(cfg).write(file_key, body, content_type="text/markdown")

    with database.transaction(cfg) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO digests (id, scope_type, scope_key, digest_date,"
            " generated_at, trigger, content, file_path) VALUES (?,?,?,?,?,?,?,?)",
            (
                digest.id,
                "person",
                key,
                digest.digest_date,
                digest.generated_at,
                trigger,
                digest.model_dump_json(),
                document.location or file_key,
            ),
        )

    should_post = cfg.post_person_digests if post is None else post
    if should_post:
        get_notifier(cfg).post(
            f"person:{key}", f"{digest.display_name} — {digest.digest_date}", body
        )

    logger.info(
        "person digest for %s on %s: %s commitment(s)",
        key, digest.digest_date, len(digest.commitments),
    )
    return digest


def emit_all_people(
    settings: Settings | None = None,
    *,
    now: datetime | None = None,
    trigger: str = "manual",
    post: bool | None = None,
) -> list[PersonDigest]:
    """One digest per person who has something. Nobody else gets one."""
    cfg = settings or get_settings()
    written = []
    for person in people(cfg):
        digest = emit_person_digest(person.key, cfg, now=now, trigger=trigger, post=post)
        if digest is not None:
            written.append(digest)
    return written
