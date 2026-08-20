"""M3 - extracting action items from a transcript.

The pipeline, in order:

    consent gate -> chunk -> per chunk: model call with quote verification
    inside the retry loop -> collect candidates -> deduplicate -> store as
    pending

Two things are worth pointing at.

**The consent gate runs again here.** It already ran at ingestion, and a
database trigger enforces it underneath. This is the layer that matters for the
capability test's wording, "never sent to a model": it is the last point before
a transcript would leave the process.

**A chunk whose quotes cannot be verified is not discarded.** The quote
validator gets the full retry budget. If the model still cannot produce a
literal quote, the last schema-valid response is taken and the offending items
are stored with `quote_verified = 0`, flagged in the queue and blocked from
approval without an explicit override. Discarding them instead would make the
fabricated-quote metric zero by construction, and the brief warns that a
harness reporting everything passing usually means the cases were too easy.
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone

from app.config import Settings, get_settings
from app.db import database
from app.db.repositories import extractions as extraction_repo
from app.db.repositories import segments as segment_repo
from app.db.repositories import sources as source_repo
from app.errors import ConsentRefused, LLMError, NotFoundError
from app.extraction.chunker import chunk_segments
from app.extraction.dates import resolve_due_date
from app.extraction.deduplicator import Candidate, dedup_key, deduplicate
from app.extraction.llm.client import call_structured
from app.extraction.llm.factory import get_llm_provider
from app.extraction.prompts import load_prompt
from app.extraction.quote_verifier import locate_quote, rejection_message, verify_quote
from app.models.chunk import Chunk
from app.models.common import UNSPECIFIED, ExtractionType, SourceStatus, StrictModel
from app.models.extraction import DraftAction, DraftActionList, Extraction

logger = logging.getLogger("agent.extract")

PROMPT_NAME = "extract_actions"


class ExtractionRun(StrictModel):
    """What one extraction run did. Returned by the API and printed by the CLI."""

    source_id: str
    extraction_type: ExtractionType
    prompt_version: str
    provider: str
    model: str

    chunks: int = 0
    candidates: int = 0
    duplicates_removed: int = 0
    stored: int = 0

    verified_quotes: int = 0
    unverified_quotes: int = 0
    unspecified_owner: int = 0
    unspecified_due_date: int = 0
    dates_resolved: int = 0

    failed_chunks: list[str] = []
    duration_ms: int = 0

    @property
    def ok(self) -> bool:
        return not self.failed_chunks


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _quote_validator(source_text: str):
    """The check that makes the retry loop worth having.

    Returns a message the model can act on: which quote failed, and how far
    into it the text stopped matching.
    """

    def validate(value: DraftActionList) -> str | None:
        for action in value.actions:
            if not verify_quote(action.verbatim_quote, source_text):
                return rejection_message(action.verbatim_quote, source_text)
        return None

    return validate


def _extract_chunk(
    chunk: Chunk, source_text: str, prompt, settings: Settings, source_id: str
) -> tuple[list[DraftAction], bool]:
    """Return (actions, quotes_were_verified) for one chunk.

    The second element is False when the quote validator was exhausted and the
    last schema-valid response was accepted anyway.
    """
    rendered = prompt.render(context=chunk.context, chunk=chunk.text)

    try:
        result = call_structured(
            PROMPT_NAME,
            rendered,
            DraftActionList,
            source_id=source_id,
            prompt_version=prompt.version_tag,
            validators=[_quote_validator(source_text)],
            settings=settings,
        )
        return result.actions, True
    except LLMError:
        logger.warning("quote verification exhausted on %s, keeping the flagged items", chunk.id)

    # Same prompt, so the first attempt is served from the cache: taking the
    # flagged items costs nothing extra and loses no real commitments.
    result = call_structured(
        PROMPT_NAME,
        rendered,
        DraftActionList,
        source_id=source_id,
        prompt_version=prompt.version_tag,
        settings=settings,
        max_retries=1,
    )
    return result.actions, False


def extract_actions(
    source_id: str,
    settings: Settings | None = None,
    *,
    replace_pending: bool = True,
) -> ExtractionRun:
    """Run M3 over one ingested source and leave the results in the queue."""
    cfg = settings or get_settings()
    started = time.perf_counter()

    with database.connect(cfg) as conn:
        source = source_repo.get_source(conn, source_id)
        if source is None:
            raise NotFoundError(f"no source with id {source_id}")
        segments = segment_repo.list_segments(conn, source_id)
        source_text = segment_repo.get_source_text(conn, source_id)

    # --- M2, the last gate before anything leaves the process ---------------
    if not source.consent_flag:
        raise ConsentRefused(
            f"source {source_id} withheld consent. Nothing is sent to a model."
        )
    if source.status is not SourceStatus.INGESTED:
        raise NotFoundError(
            f"source {source_id} has status '{source.status.value}' and has no stored segments"
        )

    prompt = load_prompt(PROMPT_NAME)
    provider = get_llm_provider(cfg)
    chunks = chunk_segments(source, segments, cfg)

    run = ExtractionRun(
        source_id=source_id,
        extraction_type=ExtractionType.ACTION,
        prompt_version=prompt.version_tag,
        provider=provider.name,
        model=provider.model,
        chunks=len(chunks),
    )

    # --- per chunk ----------------------------------------------------------
    candidates: list[Candidate] = []
    drafts: dict[str, tuple[DraftAction, Chunk, bool]] = {}

    for chunk in chunks:
        try:
            actions, verified = _extract_chunk(chunk, source_text, prompt, cfg, source_id)
        except Exception as exc:
            logger.warning("chunk %s failed: %s", chunk.id, exc)
            run.failed_chunks.append(chunk.id)
            continue

        for action in actions:
            key = str(uuid.uuid4())
            drafts[key] = (action, chunk, verified)
            candidates.append(
                Candidate(
                    key=key,
                    quote=action.verbatim_quote,
                    task=action.what,
                    confidence=action.confidence,
                    owner=action.owner,
                    location=locate_quote(action.verbatim_quote, source_text, segments),
                )
            )

    run.candidates = len(candidates)

    # --- deduplicate across chunk overlaps ----------------------------------
    survivors, merges = deduplicate(candidates)
    run.duplicates_removed = len(candidates) - len(survivors)
    merge_by_key = {m.survivor_key: m for m in merges}

    # --- store as pending ---------------------------------------------------
    stored: list[Extraction] = []
    with database.transaction(cfg) as conn:
        if replace_pending:
            extraction_repo.delete_for_source(conn, source_id, ExtractionType.ACTION)

        for candidate in survivors:
            action, chunk, chunk_verified = drafts[candidate.key]
            location = candidate.location
            verified = bool(location) and chunk_verified

            due = resolve_due_date(action.due_date, source.meeting_date)
            payload = {
                "what": action.what,
                "owner": action.owner,
                "due_date": due.value,
                "due_date_type": due.date_type.value,
                "due_date_stated": due.stated_text,
                "due_date_rule": due.rule,
            }

            merge = merge_by_key.get(candidate.key)
            extraction = Extraction(
                id=f"{source_id}::action::{candidate.key[:8]}",
                source_id=source_id,
                extraction_type=ExtractionType.ACTION,
                payload=payload,
                original_payload=payload,
                verbatim_quote=action.verbatim_quote,
                quote_verified=verified,
                quote_location=location,
                speaker=action.speaker,
                timestamp=action.timestamp,
                confidence=action.confidence,
                dedup_key=dedup_key(action.verbatim_quote, action.what),
                chunk_id=chunk.id,
                merged_from=merge.absorbed_keys if merge else [],
                provider=provider.name,
                model_name=provider.model,
                prompt_version=prompt.version_tag,
                created_at=_now(),
            )

            extraction_repo.insert(
                conn,
                extraction,
                merge_reason=merge.reason if merge else None,
                expiry_hours=cfg.pending_expiry_hours,
            )
            stored.append(extraction)

            run.stored += 1
            run.verified_quotes += int(verified)
            run.unverified_quotes += int(not verified)
            run.unspecified_owner += int(action.owner == UNSPECIFIED)
            run.unspecified_due_date += int(due.value == UNSPECIFIED)
            run.dates_resolved += int(due.date_type.value == "relative_resolved")

    run.duration_ms = int((time.perf_counter() - started) * 1000)
    logger.info(
        "extracted %s action(s) from %s: %s verified, %s unverified, %s duplicate(s) removed",
        run.stored, source_id, run.verified_quotes, run.unverified_quotes, run.duplicates_removed,
    )
    return run
