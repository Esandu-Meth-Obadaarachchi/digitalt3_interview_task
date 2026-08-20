"""Turning parsed lines into stored segments with citable character offsets.

There is one definition of "the text of a source", and everything downstream
depends on it agreeing exactly:

    source_text = " ".join(whitespace-normalised segment texts, in order)

Quote verification checks a model's quote is a substring of that string.
`char_start` and `char_end` on each segment are offsets into that same string.
Because both use one definition, an offset is directly checkable by hand and a
quote spanning two segments still verifies.

Speaker labels and timestamps are deliberately excluded from the source text.
A quote must be words somebody actually said, not "[00:02:17] Priya Sharma:".
"""

from __future__ import annotations

from app.models.ingestion import RawSegment
from app.models.source import Segment

#: Segments are joined by a single space when forming the source text.
JOIN = " "


def normalise_text(text: str) -> str:
    """Collapse every run of whitespace to one space.

    Applied identically to stored segment text and to any quote checked
    against it, so line wrapping in the source file never breaks a match.
    """
    return " ".join(text.split())


def segment_id(source_id: str, index: int) -> str:
    """Readable and stable, so it works as a citation on its own."""
    return f"{source_id}::seg{index:04d}"


def normalise(source_id: str, raw_segments: list[RawSegment]) -> tuple[list[Segment], str]:
    """Return (segments with offsets, the source text those offsets index into)."""
    segments: list[Segment] = []
    cursor = 0
    pieces: list[str] = []

    for index, raw in enumerate(raw_segments):
        text = normalise_text(raw.text)
        if not text:
            continue

        if pieces:
            cursor += len(JOIN)

        segments.append(
            Segment(
                id=segment_id(source_id, index),
                source_id=source_id,
                segment_index=index,
                speaker=raw.speaker,
                start_ts=raw.start_ts,
                end_ts=raw.end_ts,
                start_seconds=raw.start_seconds,
                text=text,
                char_start=cursor,
                char_end=cursor + len(text),
            )
        )
        pieces.append(text)
        cursor += len(text)

    return segments, JOIN.join(pieces)


def build_source_text(segments: list[Segment]) -> str:
    """Rebuild the source text from stored segments, for quote verification."""
    return JOIN.join(segment.text for segment in segments)
