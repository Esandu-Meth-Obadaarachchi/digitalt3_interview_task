"""Loading the hand-labelled ground truth, and matching it to what was extracted.

The matching rule is the part worth stating plainly, because every recall and
accuracy number depends on it:

    An extraction matches a golden action when their quotes overlap - one is a
    substring of the other after whitespace normalisation - or, failing that,
    when their task descriptions share at least half their content words.

Quote overlap is the primary signal because the quote is the anchor: two items
quoting the same words are about the same moment in the meeting. The task
fallback exists because the model may quote a neighbouring sentence of the same
exchange, which is a different span but the same commitment.

The rule is deliberately generous. A generous matcher inflates recall and
deflates false positives, so both are reported side by side and neither is
quoted alone.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.config import REPO_ROOT
from app.extraction.deduplicator import task_similarity
from app.ingestion.normaliser import normalise_text
from app.models.common import StrictModel

GOLDEN_DIR = REPO_ROOT / "sample_data" / "golden"

#: Content-word containment above which two task descriptions are the same task.
TASK_MATCH_THRESHOLD = 0.5


class GoldenAction(StrictModel):
    id: str
    source_id: str
    what: str
    owner: str
    due_date: str
    verbatim_quote: str
    speaker: str
    timestamp: str
    due_date_type: str | None = None
    notes: str | None = None


def load_actions(sources: set[str] | None = None) -> list[GoldenAction]:
    raw = json.loads((GOLDEN_DIR / "golden_actions.json").read_text(encoding="utf-8"))
    actions = [GoldenAction(**item) for item in raw["actions"]]
    if sources is not None:
        actions = [a for a in actions if a.source_id in sources]
    return actions


def load_json(name: str) -> dict:
    return json.loads((GOLDEN_DIR / name).read_text(encoding="utf-8"))


def quotes_overlap(left: str, right: str) -> bool:
    a, b = normalise_text(left).lower(), normalise_text(right).lower()
    if not a or not b:
        return False
    return a in b or b in a


def matches(golden: GoldenAction, quote: str, task: str) -> bool:
    if quotes_overlap(golden.verbatim_quote, quote):
        return True
    return task_similarity(golden.what, task) >= TASK_MATCH_THRESHOLD


class Pairing(StrictModel):
    """The result of aligning extractions with the golden set."""

    matched: dict[str, str] = {}          # golden id -> extraction id
    missed: list[str] = []                # golden ids with no extraction
    false_positives: list[str] = []       # extraction ids matching no golden action

    @property
    def recall(self) -> float:
        total = len(self.matched) + len(self.missed)
        return len(self.matched) / total if total else 0.0

    @property
    def precision(self) -> float:
        total = len(self.matched) + len(self.false_positives)
        return len(self.matched) / total if total else 0.0


def pair(golden: list[GoldenAction], extractions: list) -> Pairing:
    """Align one-to-one, in two passes.

    Pass one matches on quote overlap only, which is the strong signal: two
    items quoting the same words are about the same moment in the meeting.
    Pass two applies the task fallback to whatever is left.

    Two passes rather than one, because a single greedy pass lets an early
    golden action claim an extraction on the weak signal that a later golden
    action would have claimed on the strong one, which understates recall
    without any extraction being wrong.

    One-to-one throughout: allowing one extraction to satisfy two golden
    actions would let a single vague item take credit for several commitments.
    """
    result = Pairing()
    used: set[str] = set()
    unmatched = list(golden)

    for strong_only in (True, False):
        remaining: list[GoldenAction] = []
        for item in unmatched:
            for extraction in extractions:
                if extraction.id in used:
                    continue
                task = extraction.payload.get("what", "")
                hit = (
                    quotes_overlap(item.verbatim_quote, extraction.verbatim_quote)
                    if strong_only
                    else matches(item, extraction.verbatim_quote, task)
                )
                if hit:
                    result.matched[item.id] = extraction.id
                    used.add(extraction.id)
                    break
            else:
                remaining.append(item)
        unmatched = remaining

    result.missed = [item.id for item in unmatched]
    result.false_positives = [e.id for e in extractions if e.id not in used]
    return result


# =============================================================================
# Decisions and risks
# =============================================================================


class GoldenDecision(StrictModel):
    id: str
    source_id: str
    what_was_decided: str
    verbatim_quote: str
    who_stated_it: str | None = None
    stated_rationale: str | None = None
    timestamp: str | None = None
    alternatives_discussed: list[str] = []


class GoldenDeferred(StrictModel):
    """A decision proposed and then explicitly put off.

    The whole point of golden case 5. These must never reach the decision log,
    which makes them the only golden records asserted by absence.
    """

    id: str
    source_id: str
    what_was_proposed: str
    verbatim_quote: str
    why_deferred: str | None = None
    who_deferred_it: str | None = None
    timestamp: str | None = None
    notes: str | None = None


class GoldenRisk(StrictModel):
    id: str
    source_id: str
    description: str
    severity: str
    verbatim_quote: str
    affected_area: str | None = None
    owner: str | None = None
    speaker: str | None = None
    timestamp: str | None = None


def load_decisions(sources: set[str] | None = None) -> tuple[list[GoldenDecision], list[GoldenDeferred]]:
    raw = load_json("golden_decisions.json")
    decided = [GoldenDecision(**item) for item in raw["decisions"]]
    deferred = [GoldenDeferred(**item) for item in raw["deferred_decisions"]]
    if sources is not None:
        decided = [d for d in decided if d.source_id in sources]
        deferred = [d for d in deferred if d.source_id in sources]
    return decided, deferred


def load_risks(sources: set[str] | None = None) -> list[GoldenRisk]:
    risks = [GoldenRisk(**item) for item in load_json("golden_risks.json")["risks"]]
    if sources is not None:
        risks = [r for r in risks if r.source_id in sources]
    return risks


def quote_present(quote: str, extractions: list) -> bool:
    """Did anything in the store quote these words?

    Overlap in either direction, because a model quoting a longer or shorter
    span of the same sentence has still recorded the same thing. Used for
    decision recall and, crucially, for asserting that a deferred item is
    absent: a looser test there is the safer one, since it makes the negative
    harder to pass by accident.
    """
    return any(quotes_overlap(quote, e.verbatim_quote) for e in extractions)
