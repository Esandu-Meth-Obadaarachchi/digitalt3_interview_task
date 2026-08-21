"""Contracts for chunked transcript text."""

from __future__ import annotations

from pydantic import Field

from app.models.common import StrictModel


class Chunk(StrictModel):
    """One window of transcript, plus the context needed to reason about it.

    `text` is the transcript lines and is the only text a quote may come from.
    `context` is background (meeting, date, participants, time range) and is
    marked non-quotable in the prompt, because a quote drawn from it would not
    be a substring of the source and would fail verification.
    """

    id: str
    source_id: str
    index: int
    total: int

    segment_ids: list[str] = Field(default_factory=list)
    overlap_segment_ids: list[str] = Field(default_factory=list)
    first_segment_index: int
    last_segment_index: int

    start_ts: str | None = None
    end_ts: str | None = None
    char_start: int
    char_end: int

    text: str
    context: str
    estimated_tokens: int

    @property
    def label(self) -> str:
        return f"chunk {self.index + 1} of {self.total}"
