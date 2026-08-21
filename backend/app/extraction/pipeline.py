"""The extraction pipeline, shared by every extraction capability.

    consent gate -> chunk -> per chunk: model call with quote verification
    inside the retry loop -> collect candidates -> deduplicate -> store pending

M3, M4, M5 and M9 differ only in which prompt they load, which contract the
model must satisfy, and how a validated item becomes a stored payload. Those
three differences are an `ExtractionSpec`. Everything else is here, once.

Written by lifting it out of `actions.py` rather than by predicting what would
be common. Deduplication thresholds, the quote-verification fallback, the
consent check and the accounting were all worked out against real model output
for actions, and copying them into three more modules would have meant four
places to fix the next thing the harness finds.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any, Generic, TypeVar

from pydantic import BaseModel

from app.config import Settings, get_settings
from app.db import database
from app.db.repositories import extractions as extraction_repo
from app.db.repositories import segments as segment_repo
from app.db.repositories import sources as source_repo
from app.errors import ConsentRefused, LLMError, NotFoundError
from app.extraction.chunker import chunk_segments
from app.extraction.deduplicator import Candidate, dedup_key, deduplicate
from app.extraction.llm.client import call_structured
from app.extraction.llm.factory import get_llm_provider
from app.extraction.prompts import Prompt, load_prompt
from app.extraction.quote_verifier import locate_quote, rejection_message, verify_quote
from app.models.chunk import Chunk
from app.models.common import UNSPECIFIED, ExtractionType, SourceStatus, StrictModel
from app.models.extraction import Extraction
from app.models.source import Source

logger = logging.getLogger("agent.extract")

TResponse = TypeVar("TResponse", bound=BaseModel)


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


class ExtractionSpec(StrictModel, Generic[TResponse]):
    """Everything that differs between one extraction capability and another.

    `payload_of` receives the validated model item and the source, and returns
    the JSON payload to store. It is where a capability puts its own work, such
    as resolving a relative date against the meeting date for actions.
    """

    model_config = {"arbitrary_types_allowed": True}

    extraction_type: ExtractionType
    prompt_name: str
    response_model: type[BaseModel]
    items_field: str

    #: Text compared when deciding whether two candidates are the same thing.
    task_of: Callable[[Any], str]
    #: Owner, for the cross-region deduplication rule. UNSPECIFIED disables it.
    owner_of: Callable[[Any], str] = lambda item: UNSPECIFIED
    #: The stored payload.
    payload_of: Callable[[Any, Source], dict]

    quote_of: Callable[[Any], str] = lambda item: item.verbatim_quote
    speaker_of: Callable[[Any], str | None] = lambda item: getattr(item, "speaker", None)
    timestamp_of: Callable[[Any], str | None] = lambda item: getattr(item, "timestamp", None)
    confidence_of: Callable[[Any], float | None] = lambda item: getattr(item, "confidence", None)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _quote_validator(spec: ExtractionSpec, source_text: str):
    """The check that makes the retry loop worth having.

    Returns a message the model can act on: which quote failed, and how far
    into it the text stopped matching.
    """

    def validate(value: BaseModel) -> str | None:
        for item in getattr(value, spec.items_field, []):
            quote = spec.quote_of(item)
            if not verify_quote(quote, source_text):
                return rejection_message(quote, source_text)
        return None

    return validate


def _extract_chunk(
    spec: ExtractionSpec,
    chunk: Chunk,
    source_text: str,
    prompt: Prompt,
    settings: Settings,
    source_id: str,
) -> tuple[list[Any], bool]:
    """Return (items, quotes_were_verified) for one chunk.

    The second element is False when the quote validator was exhausted and the
    last schema-valid response was accepted anyway. Those items are stored and
    flagged rather than discarded: discarding them would make the
    fabricated-quote metric zero by construction.
    """
    rendered = prompt.render(context=chunk.context, chunk=chunk.text)

    try:
        result = call_structured(
            spec.prompt_name,
            rendered,
            spec.response_model,
            source_id=source_id,
            prompt_version=prompt.version_tag,
            validators=[_quote_validator(spec, source_text)],
            settings=settings,
        )
        return list(getattr(result, spec.items_field, [])), True
    except LLMError:
        logger.warning("quote verification exhausted on %s, keeping the flagged items", chunk.id)

    # Same prompt, so the first attempt is served from the cache: taking the
    # flagged items costs nothing extra and loses no real content.
    result = call_structured(
        spec.prompt_name,
        rendered,
        spec.response_model,
        source_id=source_id,
        prompt_version=prompt.version_tag,
        settings=settings,
        max_retries=1,
    )
    return list(getattr(result, spec.items_field, [])), False


def load_source(source_id: str, settings: Settings) -> tuple[Source, list, str]:
    """Load a source and refuse it if consent was withheld.

    The consent gate ran at ingestion and a database trigger enforces it
    underneath. This is the layer that matters for the capability test's
    wording, "never sent to a model": the last point before a transcript would
    leave the process.
    """
    with database.connect(settings) as conn:
        source = source_repo.get_source(conn, source_id)
        if source is None:
            raise NotFoundError(f"no source with id {source_id}")
        segments = segment_repo.list_segments(conn, source_id)
        source_text = segment_repo.get_source_text(conn, source_id)

    if not source.consent_flag:
        raise ConsentRefused(
            f"source {source_id} withheld consent. Nothing is sent to a model."
        )
    if source.status is not SourceStatus.INGESTED:
        raise NotFoundError(
            f"source {source_id} has status '{source.status.value}' and has no stored segments"
        )

    return source, segments, source_text


def run_extraction(
    source_id: str,
    spec: ExtractionSpec,
    settings: Settings | None = None,
    *,
    replace_pending: bool = True,
) -> ExtractionRun:
    """Run one extraction capability over one ingested source."""
    cfg = settings or get_settings()
    started = time.perf_counter()

    source, segments, source_text = load_source(source_id, cfg)

    prompt = load_prompt(spec.prompt_name)
    provider = get_llm_provider(cfg)
    chunks = chunk_segments(source, segments, cfg)

    run = ExtractionRun(
        source_id=source_id,
        extraction_type=spec.extraction_type,
        prompt_version=prompt.version_tag,
        provider=provider.name,
        model=provider.model,
        chunks=len(chunks),
    )

    # --- per chunk ----------------------------------------------------------
    candidates: list[Candidate] = []
    drafts: dict[str, tuple[Any, Chunk, bool]] = {}

    for chunk in chunks:
        try:
            items, verified = _extract_chunk(spec, chunk, source_text, prompt, cfg, source_id)
        except Exception as exc:
            logger.warning("chunk %s failed: %s", chunk.id, exc)
            run.failed_chunks.append(chunk.id)
            continue

        for item in items:
            key = str(uuid.uuid4())
            drafts[key] = (item, chunk, verified)
            candidates.append(
                Candidate(
                    key=key,
                    quote=spec.quote_of(item),
                    task=spec.task_of(item),
                    confidence=spec.confidence_of(item) or 0.5,
                    owner=spec.owner_of(item),
                    location=locate_quote(spec.quote_of(item), source_text, segments),
                )
            )

    run.candidates = len(candidates)

    # --- deduplicate across chunk overlaps and across the meeting -----------
    survivors, merges = deduplicate(candidates)
    run.duplicates_removed = len(candidates) - len(survivors)
    merge_by_key = {m.survivor_key: m for m in merges}

    # --- store as pending ---------------------------------------------------
    with database.transaction(cfg) as conn:
        if replace_pending:
            extraction_repo.delete_for_source(conn, source_id, spec.extraction_type)

        for candidate in survivors:
            item, chunk, chunk_verified = drafts[candidate.key]
            location = candidate.location
            verified = bool(location) and chunk_verified

            payload = spec.payload_of(item, source)
            merge = merge_by_key.get(candidate.key)
            quote = spec.quote_of(item)

            extraction = Extraction(
                id=f"{source_id}::{spec.extraction_type.value}::{candidate.key[:8]}",
                source_id=source_id,
                extraction_type=spec.extraction_type,
                payload=payload,
                original_payload=payload,
                verbatim_quote=quote,
                quote_verified=verified,
                quote_location=location,
                speaker=spec.speaker_of(item),
                timestamp=spec.timestamp_of(item),
                confidence=spec.confidence_of(item),
                dedup_key=dedup_key(quote, spec.task_of(item)),
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

            run.stored += 1
            run.verified_quotes += int(verified)
            run.unverified_quotes += int(not verified)
            run.unspecified_owner += int(payload.get("owner", "") == UNSPECIFIED)
            if "due_date" in payload:
                run.unspecified_due_date += int(payload["due_date"] == UNSPECIFIED)
                run.dates_resolved += int(payload.get("due_date_type") == "relative_resolved")

    run.duration_ms = int((time.perf_counter() - started) * 1000)
    logger.info(
        "extracted %s %s(s) from %s: %s verified, %s unverified, %s duplicate(s) removed",
        run.stored, spec.extraction_type.value, source_id,
        run.verified_quotes, run.unverified_quotes, run.duplicates_removed,
    )
    return run
