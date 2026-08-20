"""The single most important check in the system.

The brief: "A substring check on every quote before a record is stored costs
you five lines and eliminates the most damaging class of failure in this
domain." Golden case 2 calls the fabricated-quote count "the single most
important number in your submission".

The check itself is small. What makes it work is that it runs against exactly
the same string the ingestion pipeline built, whitespace-normalised the same
way, so a quote wrapped across lines in the source file still matches and a
quote the model invented cannot.

Verification also yields the location of the quote, so a citation points at a
span inside the source rather than at the source.
"""

from __future__ import annotations

from app.ingestion.normaliser import normalise_text
from app.models.extraction import QuoteLocation
from app.models.source import Segment

#: Below this many characters, a matching prefix says nothing useful about
#: where the model drifted.
MIN_USEFUL_PREFIX = 12


def verify_quote(quote: str, source_text: str) -> bool:
    """Is this quote a literal substring of the source?

    Whitespace is normalised on both sides before comparing. Nothing else is
    relaxed: punctuation, casing and wording must match, because a quote that
    has been tidied is no longer verbatim.
    """
    if not quote or not quote.strip():
        return False
    return normalise_text(quote) in source_text


def locate_quote(quote: str, source_text: str, segments: list[Segment] | None = None) -> QuoteLocation | None:
    """Return where the quote sits, or None when it is not there.

    `segment_id` is the segment the quote starts in, which is what a citation
    points at. A quote spanning two segments is still located by its start,
    since that is where a reader should be sent.
    """
    normalised = normalise_text(quote)
    if not normalised:
        return None

    start = source_text.find(normalised)
    if start == -1:
        return None

    end = start + len(normalised)
    segment_id = None
    if segments:
        segment_id = next(
            (s.id for s in segments if s.char_start <= start < s.char_end),
            None,
        )

    return QuoteLocation(char_start=start, char_end=end, segment_id=segment_id)


def rejection_message(quote: str, source_text: str) -> str:
    """What the model is told when its quote fails, phrased so it can act.

    Includes the longest prefix of the quote that IS present in the source, so
    the model can see where it began to drift rather than being told only that
    it was wrong. A prefix shorter than MIN_USEFUL_PREFIX is not reported: "the
    first 1 characters match" is noise, and the quote is better described as
    absent.
    """
    normalised = normalise_text(quote)

    longest = 0
    for length in range(min(len(normalised), 200), MIN_USEFUL_PREFIX - 1, -1):
        if normalised[:length] in source_text:
            longest = length
            break

    if longest == 0:
        return (
            f"the quote {quote!r} does not appear in the transcript at all. "
            f"Copy the words exactly as they are written in the TRANSCRIPT CHUNK, "
            f"without the timestamp or the speaker prefix."
        )

    return (
        f"the quote {quote!r} is not a literal substring of the transcript. "
        f"Only the first {longest} characters match: {normalised[:longest]!r}. "
        f"The text diverges after that. Copy the words exactly."
    )
