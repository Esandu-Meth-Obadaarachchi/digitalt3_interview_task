"""The agent's toolbelt, and the rule about what is missing from it.

Every tool here is read-only, with one exception that proposes and cannot
approve. There is deliberately **no tool for approving an extraction, writing to
the tracker, or sending a follow-up**, and the absence is the point rather than
an oversight:

    The loop may look at anything and suggest anything. A person still holds
    all three gates.

An agent that could approve its own proposals would make the approval gate a
formality reachable by a model deciding it was confident. The catalogue asks for
multi-step tool use, not for autonomy over the gate, and a test asserts this
module imports neither the tracker service nor the follow-up sender.

`propose_action_item` is the one tool that writes. It writes a **pending**
extraction, which is exactly what the extraction pipeline writes, and it refuses
unless the quote it was given is a literal substring of the source. So the agent
is held to the same evidence standard as the model that reads a chunk: nothing
enters the queue without a quote somebody can check.
"""

from __future__ import annotations

import json
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone

from langchain_core.tools import tool

from app.config import get_settings
from app.db import database
from app.db.repositories import chat as chat_repo
from app.db.repositories import extractions as extraction_repo
from app.db.repositories import segments as segment_repo
from app.db.repositories import sources as source_repo
from app.ingestion.normaliser import normalise_text
from app.models.common import ExtractionType, ReviewStatus, UNSPECIFIED
from app.models.extraction import Extraction, QuoteLocation
from app.retrieval.qa import answer_question
from app.retrieval.search import search
from app.review import queue

#: Trimmed before an observation goes back to the model. A tool returning six
#: thousand characters spends the context window on one step.
MAX_OBSERVATION_CHARS = 4000

# =============================================================================
# Scope: metadata filtering, enforced rather than requested
# =============================================================================
# A person running the loop can point it at one project. That is not a hint in
# the prompt: the tools read the scope themselves, so an agent that forgets the
# instruction still cannot read another project's transcripts.
#
# Two levels, and the relationship between them is the whole design:
#
#   ceiling   set by the caller. The agent cannot widen it, ever.
#   focus     set by the agent with focus_on_source, to narrow within the
#             ceiling while it works.
#
# The agent may narrow and may never widen, which is the same shape as every
# other rule here: the machine gets freedom inside a boundary a person drew.

_ceiling: ContextVar[frozenset[str] | None] = ContextVar("agent_scope_ceiling", default=None)
_focus: ContextVar[frozenset[str] | None] = ContextVar("agent_scope_focus", default=None)


def set_scope(sources: set[str] | None) -> None:
    """The caller's ceiling. None means every source."""
    _ceiling.set(frozenset(sources) if sources else None)
    _focus.set(None)


def current_scope() -> frozenset[str] | None:
    """What the tools are allowed to read right now."""
    return _focus.get() or _ceiling.get()


def scope_label() -> str:
    scope = current_scope()
    return "every source" if scope is None else ", ".join(sorted(scope))


def _trim(text: str) -> str:
    if len(text) <= MAX_OBSERVATION_CHARS:
        return text
    return text[:MAX_OBSERVATION_CHARS] + f"\n… trimmed, {len(text) - MAX_OBSERVATION_CHARS} more characters"


# =============================================================================
# Reading
# =============================================================================


@tool
def list_sources() -> str:
    """List every ingested source: meetings and chat exports, with their status.

    Use this first when the instruction names a meeting loosely, to find its id.
    """
    scope = current_scope()
    with database.connect(get_settings()) as conn:
        rows = source_repo.list_sources(conn)
    if scope is not None:
        rows = [s for s in rows if s.id in scope]
    if not rows:
        return "no sources are stored" if scope is None else f"nothing in scope ({scope_label()})"
    return "\n".join(
        f"{s.id} | {s.source_type.value} | {s.status.value} | {s.title}"
        + (f" | {s.meeting_date}" if s.meeting_date else "")
        for s in rows
    )


@tool
def search_transcripts(query: str, limit: int = 6) -> str:
    """Search MEETING transcripts and approved extractions for a phrase or topic.

    Returns numbered passages with their meeting, speaker and timestamp. Use it
    to find where something was discussed before quoting it.

    This does NOT cover chat channels. For those use search_chat_messages, and
    use both when a question could be answered from either.
    """
    hits = search(query, get_settings(), limit=limit, sources=current_scope())
    if not hits:
        return f"nothing matched {query!r} within {scope_label()}"
    return _trim(
        "\n\n".join(
            f"[{i}] {h.source_title or h.source_id} | {h.speaker or 'unattributed'} at "
            f"{h.timestamp or 'no timestamp'}\n{h.text}"
            for i, h in enumerate(hits, start=1)
        )
    )


@tool
def read_transcript(source_id: str, start_segment: int = 0, count: int = 20) -> str:
    """Read consecutive turns from one transcript, in order.

    Use it after search when a passage needs its surrounding context, or to read
    a short meeting end to end.
    """
    scope = current_scope()
    if scope is not None and source_id not in scope:
        return f"REFUSED: {source_id} is outside this run's scope ({scope_label()})"
    with database.connect(get_settings()) as conn:
        segments = segment_repo.list_segments(conn, source_id)
    if not segments:
        return f"no segments stored for {source_id}"
    window = segments[start_segment : start_segment + count]
    body = "\n".join(
        f"[{s.segment_index}] {s.start_ts or ''} {s.speaker or 'unattributed'}: {s.text}"
        for s in window
    )
    return _trim(f"{len(segments)} segments in total, showing {len(window)}\n{body}")


@tool
def list_extractions(source_id: str = "", extraction_type: str = "", status: str = "") -> str:
    """List extracted actions, decisions, risks or signals, filtered.

    extraction_type: action, decision, risk or signal. status: pending,
    approved, rejected or expired. Empty means no filter.
    """
    scope = current_scope()
    if scope is not None and source_id and source_id not in scope:
        return f"REFUSED: {source_id} is outside this run's scope ({scope_label()})"
    with database.connect(get_settings()) as conn:
        rows = extraction_repo.list_extractions(
            conn,
            source_id=source_id or None,
            extraction_type=ExtractionType(extraction_type) if extraction_type else None,
            status=ReviewStatus(status) if status else None,
        )
    if scope is not None:
        rows = [r for r in rows if r.source_id in scope]
    if not rows:
        return "no extractions match that filter"
    return _trim(
        "\n".join(
            f"{e.id} | {e.extraction_type.value} | {e.status.value} | "
            f"{json.dumps(e.payload, ensure_ascii=False)[:180]}"
            for e in rows
        )
    )


@tool
def review_queue_summary() -> str:
    """How much is waiting for a human, by status and by type."""
    summary = queue.summary(get_settings())
    return json.dumps(summary.model_dump(), indent=1)


@tool
def search_chat_messages(query: str, limit: int = 10) -> str:
    """Search stored chat messages for a word or phrase, across every channel.

    Chat lives in its own index and the transcript search cannot see it, so a
    question about something raised in a channel needs this tool.
    """
    scope = current_scope()
    clause, params = "", []
    if scope is not None:
        clause = " AND m.source_id IN (" + ",".join("?" for _ in scope) + ")"
        params = sorted(scope)

    with database.connect(get_settings()) as conn:
        rows = conn.execute(
            "SELECT m.external_id, m.channel, m.author, m.ts, m.classification, m.text"
            " FROM chat_messages_fts f JOIN chat_messages m ON m.rowid = f.rowid"
            " WHERE chat_messages_fts MATCH ?" + clause + " ORDER BY rank LIMIT ?",
            (query, *params, limit),
        ).fetchall()

    if not rows:
        return f"no stored chat message matches {query!r} within {scope_label()}"
    return _trim(
        "\n".join(
            f"{r['external_id']} | #{r['channel']} | {r['author']} at {r['ts']} | "
            f"{r['classification'] or 'unclassified'}\n{r['text']}"
            for r in rows
        )
    )


@tool
def read_chat_messages(channel: str = "", classification: str = "", limit: int = 30) -> str:
    """Read classified chat messages. classification: blocker, request, decision or question.

    Noise is never stored, so anything returned here survived classification.
    """
    scope = current_scope()
    with database.connect(get_settings()) as conn:
        rows = chat_repo.list_messages(
            conn,
            channel=channel or None,
            classification=classification or None,
            limit=limit,
        )
    if scope is not None:
        rows = [r for r in rows if r.source_id in scope]
    if not rows:
        return f"no stored messages match that filter within {scope_label()}"
    return _trim(
        "\n".join(
            f"{m.external_id} | #{m.channel} | {m.author} | "
            f"{m.classification.value if m.classification else 'unclassified'} | {m.text}"
            for m in rows
        )
    )


@tool
def answer_with_citations(question: str) -> str:
    """Ask the question-answering pipeline, which verifies every quote it returns.

    Prefer this over search_transcripts when the instruction is a question of
    fact, because the answer comes back already checked against its sources.
    """
    answer = answer_question(question, get_settings(), sources=current_scope())
    if not answer.found:
        return f"NOT FOUND: {answer.answer}"
    lines = [answer.answer, ""]
    for claim in answer.claims:
        c = claim.citation
        lines.append(
            f"- {claim.statement}\n  \"{c.quote}\"\n  {c.speaker or 'unattributed'}, "
            f"{c.source_title or c.source_id} at {c.timestamp or 'no timestamp'}"
        )
    return _trim("\n".join(lines))


@tool
def list_tracker_items() -> str:
    """What the tracker holds, marking which items the agent's pipeline created."""
    from app.adapters.factory import get_tracker

    items = get_tracker(get_settings()).list_items()
    if not items:
        return "the tracker is empty"
    return _trim(
        "\n".join(
            f"{i.external_ref} | {i.assignee or 'unassigned'} | {i.status} | {i.title}"
            for i in items
        )
    )


@tool
def focus_on_source(source_id: str) -> str:
    """Narrow every later tool call to one source, for the rest of this run.

    Use it once you know which meeting or export the instruction is about, so
    later searches are not diluted by the others. Pass an empty string to widen
    again to whatever the run was given.

    You cannot focus on something outside the scope this run was started with.
    """
    ceiling = _ceiling.get()

    if not source_id.strip():
        _focus.set(None)
        return f"focus cleared. Back to this run's scope: {scope_label()}"

    if ceiling is not None and source_id not in ceiling:
        return (
            f"REFUSED: {source_id} is outside this run's scope ({', '.join(sorted(ceiling))}). "
            f"A run can be narrowed and never widened."
        )

    with database.connect(get_settings()) as conn:
        source = source_repo.get_source(conn, source_id)
    if source is None:
        return f"REFUSED: no source with id {source_id}"

    _focus.set(frozenset({source_id}))
    return f"focused on {source_id} ({source.title}). Later calls see only this source."


# =============================================================================
# Proposing, which is the only thing here that writes, and it writes pending
# =============================================================================


@tool
def propose_action_item(source_id: str, what: str, verbatim_quote: str, owner: str = "") -> str:
    """Propose an action item into the review queue for a human to approve.

    The quote must be copied character for character from the source. A quote
    the source does not contain is refused, and the proposal is not stored.

    Leave owner empty when nobody was named. Never guess an owner.
    """
    cfg = get_settings()
    scope = current_scope()
    if scope is not None and source_id not in scope:
        return f"REFUSED: {source_id} is outside this run's scope ({scope_label()})"
    with database.connect(cfg) as conn:
        source = source_repo.get_source(conn, source_id)
        if source is None:
            return f"REFUSED: no source with id {source_id}"
        haystack = normalise_text(segment_repo.get_source_text(conn, source_id))

    needle = normalise_text(verbatim_quote)
    if not needle or needle not in haystack:
        return (
            "REFUSED: that quote does not appear in the source. Copy it character for "
            "character from the transcript, or propose nothing."
        )

    position = haystack.find(needle)
    payload = {
        "what": what,
        "owner": owner.strip() or UNSPECIFIED,
        "due_date": UNSPECIFIED,
        "due_date_type": "unspecified",
        "due_date_stated": None,
        "due_date_rule": None,
        "proposed_by": "agent",
    }
    extraction = Extraction(
        id=f"{source_id}::action::agent-{uuid.uuid4().hex[:8]}",
        source_id=source_id,
        extraction_type=ExtractionType.ACTION,
        payload=payload,
        original_payload=payload,
        verbatim_quote=verbatim_quote,
        quote_verified=True,
        quote_location=QuoteLocation(
            segment_id=None, char_start=position, char_end=position + len(needle)
        ),
        confidence=0.5,
        dedup_key=f"agent-{uuid.uuid4().hex[:16]}",
        provider="agent",
        model_name="tool-loop",
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    with database.transaction(cfg) as conn:
        extraction_repo.insert(conn, extraction, expiry_hours=cfg.pending_expiry_hours)

    return (
        f"proposed {extraction.id} into the review queue with status pending. "
        f"It is not approved and nothing has been written anywhere."
    )


#: The toolbelt, in the order a first-time reader should meet it.
TOOLS = [
    list_sources,
    search_transcripts,
    read_transcript,
    list_extractions,
    review_queue_summary,
    search_chat_messages,
    read_chat_messages,
    answer_with_citations,
    list_tracker_items,
    focus_on_source,
    propose_action_item,
]

TOOLS_BY_NAME = {t.name: t for t in TOOLS}
