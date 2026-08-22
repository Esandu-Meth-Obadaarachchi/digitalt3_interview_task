"""HTTP surface for M10.

Posting a digest is not an external write, so there is no approval gate here.
The gate is upstream: every line in a digest came from something a human
already approved, and a second gate would ask a reviewer to approve their own
earlier approvals.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Query

from app.adapters.factory import get_notifier
from app.adapters.notifier import Notification
from app.config import get_settings
from app.db import database
from app.models.digest import Digest, PersonDigest
from app.scheduler import jobs
from app.scheduler.digest import build_digest, emit_all, emit_digest, scopes
from app.scheduler.person_digest import build_person_digest, emit_all_people, emit_person_digest, people

router = APIRouter(prefix="/api/digests", tags=["digests"])


@router.get("/schedule", response_model=jobs.SchedulerStatus, summary="What the scheduler holds")
def schedule() -> jobs.SchedulerStatus:
    """Next fire times come from the scheduler, not from configuration.

    A time read back from settings would prove only that settings can be read.
    A next-run timestamp that advances on its own is the difference between a
    scheduler and a constant.
    """
    return jobs.status(get_settings())


@router.get("/scopes", summary="Every scope a digest can be produced for")
def list_scopes() -> list[dict[str, str]]:
    return [{"key": key, "title": title} for key, title in scopes(get_settings())]


# --- M13, per-person digests -------------------------------------------------
# Declared before /{scope_key}, because a path parameter matching a single
# segment would otherwise swallow /people.


@router.get("/people", summary="Everyone a digest can be produced for")
def list_people() -> list[dict]:
    """Who has approved commitments, and what each grouping covers.

    `aliases` is every owner string collapsed into the person, and `ambiguous`
    says two different full names share the first name. Both are returned
    rather than hidden, so the interface shows the grouping instead of asking
    anybody to take it on trust.
    """
    return [
        {
            "key": person.key,
            "display_name": person.display_name,
            "aliases": person.aliases,
            "ambiguous": person.ambiguous,
            "unassigned": person.unassigned,
        }
        for person in people(get_settings())
    ]


@router.get("/people/{key}", response_model=PersonDigest, summary="Preview one person's digest")
def preview_person(
    key: str,
    now: datetime | None = Query(default=None, description="clock override"),
) -> PersonDigest:
    return build_person_digest(
        key, get_settings(), now=now, trigger="clock_override" if now else "manual"
    )


@router.get("/people/{key}/markdown", summary="One person's digest as they would read it")
def person_markdown(key: str, now: datetime | None = Query(default=None)) -> dict[str, str]:
    digest = build_person_digest(key, get_settings(), now=now)
    if digest.empty:
        # M13: a person with no commitments gets no digest. Returning an empty
        # document would be a digest.
        raise HTTPException(status_code=404, detail=f"no approved commitments for {key}")
    return {"person_key": key, "digest_date": digest.digest_date, "markdown": digest.render()}


@router.post("/people/{key}", response_model=PersonDigest, summary="Write one person's digest")
def emit_person(
    key: str,
    now: datetime | None = Query(default=None),
    post: bool | None = Query(default=None),
) -> PersonDigest:
    digest = emit_person_digest(
        key, get_settings(), now=now, trigger="clock_override" if now else "manual", post=post
    )
    if digest is None:
        raise HTTPException(status_code=404, detail=f"no approved commitments for {key}")
    return digest


@router.post("/people/run/all", response_model=list[PersonDigest],
             summary="Write a digest for everyone who has one")
def run_all_people(now: datetime | None = Query(default=None)) -> list[PersonDigest]:
    """Nobody with an empty digest appears in the result, because nobody with
    an empty digest gets one."""
    return emit_all_people(
        get_settings(), now=now, trigger="clock_override" if now else "manual"
    )


@router.get("", summary="Digests already written")
def list_digests(scope_key: str | None = Query(default=None)) -> list[dict]:
    clauses, params = [], []
    if scope_key:
        clauses.append("scope_key = ?")
        params.append(scope_key)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""

    with database.connect(get_settings()) as conn:
        rows = conn.execute(
            "SELECT id, scope_type, scope_key, digest_date, generated_at, trigger, file_path"
            f" FROM digests{where} ORDER BY digest_date DESC, scope_key",
            tuple(params),
        ).fetchall()
    return [dict(row) for row in rows]


@router.get("/{scope_key}", response_model=Digest, summary="Preview without writing anything")
def preview(
    scope_key: str,
    now: datetime | None = Query(default=None, description="clock override, for the walkthrough"),
) -> Digest:
    """Builds a digest and returns it without storing or posting.

    `now` is the clock override the capability test asks for: it produces the
    digest for any date without waiting for six o'clock.
    """
    return build_digest(scope_key, get_settings(), now=now, trigger="clock_override" if now else "manual")


@router.post("/{scope_key}", response_model=Digest, summary="Build, store and post one digest")
def emit(
    scope_key: str,
    now: datetime | None = Query(default=None),
    post: bool = Query(default=True),
) -> Digest:
    return emit_digest(
        scope_key, get_settings(), now=now, trigger="clock_override" if now else "manual", post=post
    )


@router.post("/run/all", response_model=list[Digest], summary="Run the whole scheduled job now")
def run_all(now: datetime | None = Query(default=None)) -> list[Digest]:
    """Exactly what the scheduler runs at the configured hour.

    Same function, same arguments, different trigger label, so what is
    demonstrated is what runs unattended rather than a parallel path.
    """
    return emit_all(get_settings(), now=now, trigger="clock_override" if now else "manual")


@router.get("/posts/log", response_model=list[Notification], summary="What would have been posted")
def posts(
    channel: str | None = Query(default=None), limit: int | None = Query(default=None, ge=1, le=200)
) -> list[Notification]:
    return get_notifier(get_settings()).list_posts(channel, limit)


@router.get("/{scope_key}/markdown", summary="A digest as a person would read it")
def markdown(
    scope_key: str,
    now: datetime | None = Query(default=None),
) -> dict[str, str]:
    digest = build_digest(scope_key, get_settings(), now=now)
    if digest.empty and digest.considered == 0:
        raise HTTPException(status_code=404, detail=f"no approved items in scope for {scope_key}")
    return {"scope_key": scope_key, "digest_date": digest.digest_date, "markdown": digest.render()}
