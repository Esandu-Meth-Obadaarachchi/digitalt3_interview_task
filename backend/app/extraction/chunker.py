"""Chunking. The brief says this decides extraction quality, and that they
will ask about it.

Four properties, each chosen for a reason:

1. Chunks are built from whole segments, never split mid-segment.
   A segment is one person's turn. Splitting it separates a commitment from
   the words that make it one, and would produce a quote that cannot be a
   substring of any single line.

2. Overlap is whole segments, not a character count.
   A commitment is usually made across two turns: somebody asks, somebody
   agrees. If a chunk boundary falls between them, one chunk sees the request
   with no acceptance and the other sees an acceptance with no request. Both
   are then wrong in the same way. Repeating the last few whole turns means the
   pair appears complete in at least one chunk. Phase 3 deduplicates whatever
   the overlap causes to be extracted twice.

3. Every chunk carries a context header: the meeting, its date, the full
   participant list, and the time range the chunk covers.
   Without the participant list the model cannot tell that "James" is James
   Liu, and cannot know that two people called Priya are in the room. Owner
   attribution is golden case 3, and this is where its errors come from. The
   header is explicitly marked non-quotable, because a quote taken from it
   would fail the substring check against the transcript.

4. Timestamps and speaker labels are rendered into the chunk text.
   The model must return them, so it has to be able to see them.

Token counts are estimated as characters / 4. That is an approximation, and
naming it here is more honest than importing a tokeniser for a different model
family and implying a precision that is not there. Chunks are sized well below
any provider's context limit, so the approximation has room to be wrong.
"""

from __future__ import annotations

from app.config import Settings, get_settings
from app.models.chunk import Chunk
from app.models.source import Segment, Source

#: Average characters per token for English prose. Approximate, deliberately.
CHARS_PER_TOKEN = 4

#: Rendered in place of a missing speaker. Deliberately the same token the
#: model must output for an unknown owner, so the prompt and the transcript
#: agree on what "not stated" looks like.
UNLABELLED = "UNSPECIFIED"


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // CHARS_PER_TOKEN)


def render_segment(segment: Segment) -> str:
    timestamp = segment.start_ts or "--:--:--"
    speaker = segment.speaker or UNLABELLED
    return f"[{timestamp}] {speaker}: {segment.text}"


def build_context(source: Source, segments: list[Segment], index: int, total: int) -> str:
    """The non-quotable header prefixed to every chunk."""
    participants = ", ".join(source.participants) if source.participants else "not stated"
    first = next((s.start_ts for s in segments if s.start_ts), "--:--:--")
    last = next((s.start_ts for s in reversed(segments) if s.start_ts), "--:--:--")

    return (
        "CONTEXT (background only, never quote from this block)\n"
        f"  Meeting     : {source.title}\n"
        f"  Date        : {source.meeting_date or 'not stated'}\n"
        f"  Participants: {participants}\n"
        f"  This chunk  : {index + 1} of {total}, covering {first} to {last}\n"
        "  Note        : the participant list is who was in the room. A person\n"
        "                being present does not make them the owner of anything."
    )


def chunk_segments(
    source: Source,
    segments: list[Segment],
    settings: Settings | None = None,
    *,
    max_tokens: int | None = None,
    overlap_tokens: int | None = None,
) -> list[Chunk]:
    """Split a transcript into overlapping, context-carrying chunks."""
    cfg = settings or get_settings()
    budget = max_tokens if max_tokens is not None else cfg.chunk_max_tokens
    overlap_budget = overlap_tokens if overlap_tokens is not None else cfg.chunk_overlap_tokens

    if not segments:
        return []

    # --- pass one: decide the segment windows -------------------------------
    windows: list[tuple[int, int, int]] = []  # (start, end exclusive, overlap count)
    start = 0
    overlap_count = 0

    while start < len(segments):
        used = 0
        end = start
        while end < len(segments):
            cost = estimate_tokens(render_segment(segments[end]))
            if used and used + cost > budget:
                break
            used += cost
            end += 1

        windows.append((start, end, overlap_count))

        if end >= len(segments):
            break

        # Walk back over whole segments until the overlap budget is spent.
        overlap_start = end
        spent = 0
        while overlap_start > start + 1:
            cost = estimate_tokens(render_segment(segments[overlap_start - 1]))
            if spent + cost > overlap_budget:
                break
            spent += cost
            overlap_start -= 1

        overlap_count = end - overlap_start
        start = overlap_start

    # --- pass two: build the chunks now the total is known -------------------
    total = len(windows)
    chunks: list[Chunk] = []

    for index, (window_start, window_end, carried) in enumerate(windows):
        members = segments[window_start:window_end]
        body = "\n".join(render_segment(s) for s in members)
        context = build_context(source, members, index, total)

        chunks.append(
            Chunk(
                id=f"{source.id}::chunk{index:03d}",
                source_id=source.id,
                index=index,
                total=total,
                segment_ids=[s.id for s in members],
                overlap_segment_ids=[s.id for s in members[:carried]],
                first_segment_index=members[0].segment_index,
                last_segment_index=members[-1].segment_index,
                start_ts=members[0].start_ts,
                end_ts=members[-1].start_ts,
                char_start=members[0].char_start,
                char_end=members[-1].char_end,
                text=body,
                context=context,
                estimated_tokens=estimate_tokens(body),
            )
        )

    return chunks
