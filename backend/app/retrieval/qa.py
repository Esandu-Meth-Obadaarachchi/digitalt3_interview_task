"""M8 - answering questions across everything stored, with citations.

    retrieve -> build numbered sources -> model answers in claims ->
    verify every quote against the source it cites -> answer, or not-found

The verification step is what makes this different from a chatbot over a
transcript. The model returns claims, each carrying a quote and the number of
the source it came from, and every quote is checked as a literal substring of
that specific source. A claim whose quote does not verify is dropped.

An answer with no surviving claims is a not-found, not a fluent paragraph. That
is the design decision behind the whole module: the system would rather say
nothing than say something a reader cannot check, because a reader cannot tell
a real answer from an invented one and will believe both.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from pydantic import Field

from app.config import Settings, get_settings
from app.errors import LLMError
from app.extraction.llm.client import call_structured
from app.extraction.llm.factory import get_llm_provider
from app.extraction.prompts import load_prompt
from app.ingestion.normaliser import normalise_text
from app.models.common import Citation, Confidence, StrictModel
from app.retrieval.search import SearchHit, search

logger = logging.getLogger("agent.qa")

PROMPT_NAME = "answer_question"

NOT_FOUND = "not found in the available sources"


# =============================================================================
# What the model returns
# =============================================================================


class DraftClaim(StrictModel):
    statement: str = Field(description="what the quote establishes, in your own words")
    source_index: int = Field(ge=1, description="which numbered source the quote came from")
    quote: str = Field(description="copied character for character from that source")


class DraftAnswer(StrictModel):
    answerable: bool
    answer: str
    claims: list[DraftClaim] = Field(default_factory=list)


# =============================================================================
# What the caller gets
# =============================================================================


class AnswerClaim(StrictModel):
    """One statement, and the citation that backs it."""

    statement: str
    citation: Citation
    verified: bool = True


class Answer(StrictModel):
    question: str
    found: bool
    answer: str
    claims: list[AnswerClaim] = Field(default_factory=list)

    retrieval_mode: str
    considered: list[SearchHit] = Field(default_factory=list)
    provider: str | None = None
    model: str | None = None
    prompt_version: str | None = None

    dropped_claims: list[str] = Field(default_factory=list)
    #: True when the model call itself failed, typically a rate limit. A
    #: not-found produced this way looks identical to a genuine refusal, and
    #: the difference matters enormously: one is the system working and the
    #: other is a measurement that never happened. The eval harness reads this
    #: to decide whether a run may be written.
    model_failed: bool = False
    duration_ms: int = 0
    answered_at: str = ""

    @property
    def citations(self) -> list[Citation]:
        return [claim.citation for claim in self.claims]


def render_sources(hits: list[SearchHit]) -> str:
    """Number the retrieved passages for the model to cite.

    Every passage carries its meeting, speaker and timestamp, because the model
    is asked when things happened and cannot answer that from the words alone.
    """
    blocks = []
    for index, hit in enumerate(hits, start=1):
        blocks.append(
            f"[{index}] meeting: {hit.source_title or hit.source_id}\n"
            f"    speaker: {hit.speaker or 'not attributed'}   at: {hit.timestamp or 'no timestamp'}\n"
            f"    text: {hit.text}"
        )
    return "\n\n".join(blocks)


def _not_found(
    question: str,
    mode: str,
    hits: list[SearchHit],
    reason: str,
    started: float,
    *,
    model_failed: bool = False,
) -> Answer:
    return Answer(
        question=question,
        found=False,
        answer=f"{NOT_FOUND}. {reason}".strip(),
        retrieval_mode=mode,
        considered=hits,
        model_failed=model_failed,
        duration_ms=int((time.perf_counter() - started) * 1000),
        answered_at=datetime.now(timezone.utc).isoformat(),
    )


def answer_question(
    question: str,
    settings: Settings | None = None,
    *,
    mode: str | None = None,
    limit: int | None = None,
    sources: set[str] | None = None,
) -> Answer:
    """Answer from stored content, or say it cannot be answered.

    `sources` narrows retrieval by metadata before ranking, so a scoped run
    gets the best passages in scope rather than the best in the corpus that
    happen to be in scope.
    """
    cfg = settings or get_settings()
    started = time.perf_counter()
    chosen_mode = mode or cfg.retrieval_mode

    hits = search(question, cfg, mode=chosen_mode, limit=limit, sources=sources)
    if not hits:
        return _not_found(
            question, chosen_mode, hits,
            "Nothing in the stored transcripts or approved extractions matched this question.",
            started,
        )

    prompt = load_prompt(PROMPT_NAME)
    provider = get_llm_provider(cfg)

    def quotes_must_come_from_the_source_cited(value: DraftAnswer) -> str | None:
        """The check that makes a citation worth reading.

        Verified against the specific source cited, not against the corpus. A
        quote that appears in source 4 while the claim cites source 2 is a
        citation nobody can follow, and it would pass a check against the
        corpus as a whole.
        """
        for claim in value.claims:
            if not 1 <= claim.source_index <= len(hits):
                return (
                    f"source_index {claim.source_index} does not exist. "
                    f"There are {len(hits)} sources, numbered 1 to {len(hits)}."
                )
            cited = normalise_text(hits[claim.source_index - 1].text)
            if normalise_text(claim.quote) not in cited:
                return (
                    f"the quote {claim.quote!r} does not appear in source "
                    f"{claim.source_index}. Copy the words exactly from the source you cite."
                )
        return None

    try:
        draft = call_structured(
            PROMPT_NAME,
            prompt.render(question=question, sources=render_sources(hits)),
            DraftAnswer,
            prompt_version=prompt.version_tag,
            validators=[quotes_must_come_from_the_source_cited],
            settings=cfg,
        )
    except LLMError as exc:
        logger.warning("question answering failed: %s", exc)
        return _not_found(
            question, chosen_mode, hits,
            "The model could not produce a citable answer for this question.",
            started,
            model_failed=True,
        )

    if not draft.answerable:
        answer = _not_found(question, chosen_mode, hits, draft.answer, started)
        answer.provider, answer.model = provider.name, provider.model
        answer.prompt_version = prompt.version_tag
        return answer

    # --- verify again, after the retry loop --------------------------------
    # The validator already ran inside the loop. Re-checking here means the
    # stored answer is verified by code that did not also produce it, and it
    # catches the case where a final attempt was accepted despite a failure.
    claims: list[AnswerClaim] = []
    dropped: list[str] = []

    for claim in draft.claims:
        if not 1 <= claim.source_index <= len(hits):
            dropped.append(f"cited source {claim.source_index}, which does not exist")
            continue

        hit = hits[claim.source_index - 1]
        if normalise_text(claim.quote) not in normalise_text(hit.text):
            dropped.append(f"quote not found in source {claim.source_index}: {claim.quote[:60]!r}")
            continue

        claims.append(
            AnswerClaim(
                statement=claim.statement,
                citation=Citation(
                    source_id=hit.source_id,
                    source_title=hit.source_title,
                    segment_id=hit.ref_id if hit.ref_type == "segment" else None,
                    speaker=hit.speaker,
                    timestamp=hit.timestamp,
                    quote=claim.quote,
                    char_start=hit.char_start,
                    char_end=hit.char_end,
                ),
            )
        )

    if not claims:
        # An answer whose every claim failed verification is not an answer.
        answer = _not_found(
            question, chosen_mode, hits,
            "The model produced an answer but none of its citations could be verified.",
            started,
        )
        answer.dropped_claims = dropped
        answer.provider, answer.model = provider.name, provider.model
        answer.prompt_version = prompt.version_tag
        return answer

    return Answer(
        question=question,
        found=True,
        answer=draft.answer,
        claims=claims,
        retrieval_mode=chosen_mode,
        considered=hits,
        provider=provider.name,
        model=provider.model,
        prompt_version=prompt.version_tag,
        dropped_claims=dropped,
        duration_ms=int((time.perf_counter() - started) * 1000),
        answered_at=datetime.now(timezone.utc).isoformat(),
    )
