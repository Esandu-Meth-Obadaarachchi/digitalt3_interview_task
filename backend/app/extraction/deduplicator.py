"""Removing the duplicates that chunk overlap deliberately creates.

Overlap exists so that a commitment made across two speaker turns appears
complete in at least one chunk. The price is that the same commitment is
extracted more than once, and paying that price knowingly is better than
missing commitments at chunk boundaries.

Two candidates are the same commitment when BOTH signals agree:

  same region  their quotes are identical after whitespace normalisation, or
               their quote spans overlap by at least half the shorter span
  same task    the content words of the shorter task description are largely
               contained in the longer one

Both are required. Region alone is not enough: "I'll write the tests and Priya
will review the schema" carries two different commitments, and a model
extracting each of them quotes the same sentence for both. Task alone is not
enough either: two people can commit to similar-sounding work in different
parts of a meeting.

The region signal needs the second because the model rarely quotes exactly the
same span twice. Given one sentence in two chunks it will quote a slightly
longer or shorter piece of it, so exact quote equality alone misses real
duplicates.

When the two signals disagree, both candidates are kept. A duplicate sitting in
the review queue is visible and a reviewer dismisses it in one click. A pair
merged wrongly is invisible and has silently destroyed a real commitment.

The survivor is the higher-confidence candidate, kept whole. Fields are never
mixed between two candidates: a record assembled from two different model
outputs is a record no single model output ever produced, and neither its
quote nor its owner could then be defended as coming from one place.
"""

from __future__ import annotations

import hashlib
import re

from app.ingestion.normaliser import normalise_text
from app.models.common import StrictModel
from app.models.extraction import QuoteLocation

#: Fraction of the shorter quote span that must overlap. Half means one quote
#: is largely inside the other rather than merely touching it.
SPAN_OVERLAP_THRESHOLD = 0.5

#: Containment of the content words of the shorter task description in the
#: longer one. Containment rather than Jaccard, because the model describes the
#: same commitment at different lengths in different chunks and Jaccard
#: penalises that: "Finish auth refactor with integration tests" against
#: "Complete the authentication refactor and its tests" scores 0.29 by Jaccard
#: and 0.50 by containment, and they are plainly the same commitment.
TASK_SIMILARITY_THRESHOLD = 0.4

#: Containment reaches 1.0 trivially when one side has a single content word,
#: so a description below this length is not allowed to match on rule two at
#: all and can only be merged by an identical quote.
MIN_CONTENT_WORDS = 2

#: Words carrying no information about which task is being described.
_STOPWORDS = frozenset(
    """a an and are as at be by for from has have in into is it its of on or that the
    to with will shall should would need needs needed do does done make makes making
    get gets getting go goes going this these those their our his her they we you""".split()
)

_WORD = re.compile(r"[a-z0-9]+")


class Candidate(StrictModel):
    """The minimum the deduplicator needs to compare two extractions."""

    key: str
    quote: str
    task: str
    confidence: float
    location: QuoteLocation | None = None


class MergeResult(StrictModel):
    survivor_key: str
    absorbed_keys: list[str] = []
    reason: str


def content_words(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if w not in _STOPWORDS and len(w) > 2}


def task_similarity(left: str, right: str) -> float:
    """Containment of the smaller content-word set in the larger.

    Zero when either description is too short to compare, which forces those
    through the identical-quote rule instead.
    """
    a, b = content_words(left), content_words(right)
    if len(a) < MIN_CONTENT_WORDS or len(b) < MIN_CONTENT_WORDS:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def span_overlap(left: QuoteLocation | None, right: QuoteLocation | None) -> float:
    """Overlap as a fraction of the shorter span. Zero when either is missing.

    An unverified quote has no location, so it can only ever be matched by rule
    one, exact quote equality. That is deliberate: an unverified item should not
    be silently absorbed into a verified one.
    """
    if left is None or right is None:
        return 0.0

    start = max(left.char_start, right.char_start)
    end = min(left.char_end, right.char_end)
    if end <= start:
        return 0.0

    shortest = min(left.char_end - left.char_start, right.char_end - right.char_start)
    return (end - start) / shortest if shortest else 0.0


def are_duplicates(left: Candidate, right: Candidate) -> tuple[bool, str]:
    """Decide, and say why. The reason is stored on the surviving record."""
    identical = normalise_text(left.quote) == normalise_text(right.quote)
    overlap = 1.0 if identical else span_overlap(left.location, right.location)
    if overlap < SPAN_OVERLAP_THRESHOLD:
        return False, ""

    similarity = task_similarity(left.task, right.task)
    if similarity < TASK_SIMILARITY_THRESHOLD:
        return False, ""

    region = (
        "identical quote after whitespace normalisation"
        if identical
        else f"quote spans overlap by {overlap:.0%} of the shorter span"
    )
    return True, (
        f"{region}, and {similarity:.0%} of the shorter task description's content "
        f"words appear in the longer one"
    )


def deduplicate(candidates: list[Candidate]) -> tuple[list[Candidate], list[MergeResult]]:
    """Return the surviving candidates and a record of every merge.

    Candidates are considered highest confidence first, so the survivor of any
    group is the one the model was most sure of. Comparison is pairwise against
    survivors only, which is quadratic in the number of survivors and entirely
    adequate: a 25-minute meeting yields tens of candidates, not thousands.
    """
    ordered = sorted(candidates, key=lambda c: (-c.confidence, c.key))
    survivors: list[Candidate] = []
    merges: dict[str, MergeResult] = {}

    for candidate in ordered:
        for survivor in survivors:
            duplicate, reason = are_duplicates(survivor, candidate)
            if duplicate:
                record = merges.setdefault(
                    survivor.key, MergeResult(survivor_key=survivor.key, absorbed_keys=[], reason=reason)
                )
                record.absorbed_keys.append(candidate.key)
                break
        else:
            survivors.append(candidate)

    survivors.sort(key=lambda c: (c.location.char_start if c.location else 10**9, c.key))
    return survivors, list(merges.values())


def dedup_key(quote: str, task: str) -> str:
    """A stable identity for one extraction, used by the store's unique index.

    Built from the quote and the opening of the task description, so
    re-extracting the same source does not create a second row, while two
    different commitments quoted from one sentence stay distinct.
    """
    material = f"{normalise_text(quote).lower()}|{normalise_text(task).lower()[:60]}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]
