#!/usr/bin/env python
"""The evaluation harness.

Scored separately and heavily, and worth more of the total than the user
interface. It runs the golden test cases against the real pipeline and prints
one line per metric with the measured value and the target.

    make eval                 configured provider, cache allowed
    make eval-fresh           cache bypassed, to prove the numbers reproduce
    make eval-repeat          three uncached runs, reported as a range

Repeated runs exist because the model is not deterministic. Gemini returned
different action sets for the same chunk at temperature 0, minutes apart, so a
single run is a sample rather than a measurement. `--runs N` executes the whole
pipeline N times with the cache bypassed and reports the range. A target is
counted as met only when the WORST run met it: a system that sometimes clears
the bar is not a system that clears the bar.

Cases covered here (Phase 3):

  1  action recall                 target >= 0.70
  2  fabricated quotes             target = 0        the most important number
  3a owner accuracy where named    target >= 0.90
  3b UNSPECIFIED compliance        target = 2/2
  4  invented dates                target = 0
  5  deferred decision recorded    target = 0        the negative test
  5b decision recall               target >= 0.70
  M5 risk recall                   target >= 0.70
  M5b severity defensible          target = all

Reported alongside, because the brief asks for them and they are what stop a
recall number being read on its own:

  false positives, and precision, because "a system that extracts thirty
  actions to catch ten is not useful"
  precision and recall at several confidence thresholds, which shows whether
  the model's own confidence is worth anything
  retry and repair statistics, which show how often the model had to be
  corrected before it produced a usable answer

Nothing is written to eval/results.txt unless a real run produced it.
"""

from __future__ import annotations

import argparse
import io
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))
sys.path.insert(0, str(REPO_ROOT / "eval"))

from golden import (  # noqa: E402
    GoldenAction,
    GoldenRisk,
    Pairing,
    load_actions,
    load_decisions,
    load_questions,
    load_risks,
    load_signals,
    pair,
    quote_present,
    quotes_overlap,
)

from app.config import Settings, get_settings  # noqa: E402
from app.db import database  # noqa: E402
from app.db.repositories import chat as chat_repo  # noqa: E402
from app.db.repositories import extractions as extraction_repo  # noqa: E402
from app.db.repositories import llm_calls as llm_call_repo  # noqa: E402
from app.db.repositories import segments as segment_repo  # noqa: E402
from app.extraction.actions import extract_actions  # noqa: E402
from app.extraction.decisions import extract_decisions  # noqa: E402
from app.extraction.risks import extract_risks  # noqa: E402
from app.extraction.signals import classify_signals  # noqa: E402
from app.retrieval.embeddings import get_embedder  # noqa: E402
from app.retrieval.qa import answer_question  # noqa: E402
from app.retrieval.search import search  # noqa: E402
from app.retrieval.vector_index import VectorIndex, rebuild  # noqa: E402
from app.extraction.llm.factory import get_llm_provider  # noqa: E402
from app.extraction.prompts import load_prompt  # noqa: E402
from app.extraction.quote_verifier import verify_quote  # noqa: E402
from app.ingestion.service import ingest_from_manifest  # noqa: E402
from app.models.common import UNSPECIFIED, ExtractionType, StrictModel  # noqa: E402

#: The two valid transcripts. The refused and malformed sources have no
#: extractions by design, and including them would dilute every metric.
SCORED_SOURCES = {
    "meeting-sprint-planning-2024-11-18",
    "meeting-client-status-2024-08-19",
}

CONFIDENCE_THRESHOLDS = (0.0, 0.5, 0.7, 0.8, 0.9)


class MetricRange(StrictModel):
    """One metric observed across several independent runs."""

    case: str
    name: str
    target: str
    values: list[str] = []
    numeric: list[float] = []
    passes: int = 0
    total: int = 0
    targeted: bool = True

    @property
    def worst(self) -> str:
        # When every run agreed, report what they said rather than a derived
        # number: "2/2" is clearer than the 1.00 it converts to.
        if len(set(self.values)) <= 1:
            return self.values[0] if self.values else "-"
        if not self.numeric:
            return self.values[0]
        # "Worst" depends on the metric: for a count that must be zero the
        # worst run is the highest, for a rate with a floor it is the lowest.
        if self.target.startswith("="):
            return f"{max(self.numeric):g}"
        worst_index = self.numeric.index(min(self.numeric))
        return self.values[worst_index]

    @property
    def spread(self) -> str:
        if len(set(self.values)) <= 1:
            return self.values[0] if self.values else "-"
        if self.numeric:
            low, high = min(self.numeric), max(self.numeric)
            fmt = "{:g}" if self.target.startswith("=") else "{:.2f}"
            return f"{fmt.format(low)} - {fmt.format(high)}"
        return " / ".join(self.values)

    @property
    def stable(self) -> bool:
        return len(set(self.values)) <= 1

    @property
    def passed(self) -> bool | None:
        if not self.targeted:
            return None
        return self.passes == self.total


class RepeatedReport(StrictModel):
    """Several runs of the whole pipeline, reported as a range."""

    generated_at: str
    runs: int
    is_measurement: bool = True
    incomplete_reason: str | None = None
    provider: str
    model: str
    prompt_version: str
    sources: list[str]
    golden_actions: int
    extracted_per_run: list[int] = []
    metrics: list[MetricRange] = []
    usage: dict = {}

    @property
    def failed(self) -> list[MetricRange]:
        return [m for m in self.metrics if m.passed is False]

    @property
    def unstable(self) -> list[MetricRange]:
        return [m for m in self.metrics if m.targeted and not m.stable]


class Metric(StrictModel):
    case: str
    name: str
    measured: str
    target: str
    passed: bool | None = None      # None = reported, not targeted
    detail: str = ""


class EvalReport(StrictModel):
    generated_at: str
    is_measurement: bool = True   #: False when run against the deterministic stub
    #: Set when chunks failed, typically a rate limit or an exhausted quota.
    #: An incomplete run scores whatever happened to get through and is not a
    #: measurement of anything. Committing one would be fabricated results.
    incomplete_reason: str | None = None
    provider: str
    model: str
    prompt_version: str
    cache_enabled: bool
    sources: list[str]
    golden_actions: int
    extracted_actions: int
    metrics: list[Metric] = []
    calibration: list[dict] = []
    misses: list[str] = []
    false_positives: list[str] = []
    usage: dict = {}

    @property
    def failed(self) -> list[Metric]:
        return [m for m in self.metrics if m.passed is False]


# =============================================================================
# The cases
# =============================================================================


def case_1_recall(pairing: Pairing, golden: list[GoldenAction]) -> list[Metric]:
    return [
        Metric(
            case="1",
            name="Action recall",
            measured=f"{pairing.recall:.2f}",
            target=">= 0.70",
            passed=pairing.recall >= 0.70,
            detail=f"{len(pairing.matched)}/{len(golden)} hand-labelled actions found",
        ),
        Metric(
            case="1b",
            name="Precision",
            measured=f"{pairing.precision:.2f}",
            target="reported",
            detail=f"{len(pairing.false_positives)} extraction(s) matched no golden action",
        ),
    ]


def case_2_fabricated_quotes(extractions: list, source_texts: dict[str, str]) -> list[Metric]:
    """The single most important number in the submission.

    Recomputed from the stored quote and the stored transcript rather than
    trusting the quote_verified flag, so the metric does not depend on the code
    path that set it.
    """
    fabricated = [
        e for e in extractions if not verify_quote(e.verbatim_quote, source_texts.get(e.source_id, ""))
    ]
    flagged = [e for e in extractions if not e.quote_verified]

    metrics = [
        Metric(
            case="2",
            name="Fabricated quotes",
            measured=str(len(fabricated)),
            target="= 0",
            passed=len(fabricated) == 0,
            detail="recomputed against the stored transcript, not read from the stored flag",
        )
    ]
    if flagged:
        metrics.append(
            Metric(
                case="2b",
                name="Items flagged unverified",
                measured=str(len(flagged)),
                target="reported",
                detail="stored, blocked from approval without an explicit override",
            )
        )
    return metrics


def case_3_owner(pairing: Pairing, golden: list[GoldenAction], by_id: dict) -> list[Metric]:
    named_total = named_correct = 0
    unspecified_total = unspecified_correct = 0
    wrong: list[str] = []

    for item in golden:
        extraction_id = pairing.matched.get(item.id)
        if extraction_id is None:
            continue
        produced = by_id[extraction_id].payload.get("owner", UNSPECIFIED)

        if item.owner == UNSPECIFIED:
            unspecified_total += 1
            if produced == UNSPECIFIED:
                unspecified_correct += 1
            else:
                wrong.append(f"{item.id}: no owner stated, but {produced!r} was returned")
        else:
            named_total += 1
            if produced == item.owner:
                named_correct += 1
            else:
                wrong.append(f"{item.id}: expected {item.owner!r}, got {produced!r}")

    accuracy = named_correct / named_total if named_total else 0.0
    return [
        Metric(
            case="3a",
            name="Owner accuracy (named)",
            measured=f"{accuracy:.2f}",
            target=">= 0.90",
            passed=accuracy >= 0.90 if named_total else None,
            detail=f"{named_correct}/{named_total} matched actions where an owner is stated",
        ),
        Metric(
            case="3b",
            name="UNSPECIFIED compliance",
            measured=f"{unspecified_correct}/{unspecified_total}",
            target="all",
            passed=unspecified_correct == unspecified_total if unspecified_total else None,
            detail="a guessed owner counts as a failure, not a near-miss. "
            + ("; ".join(wrong[:3]) if wrong else "no owner was guessed"),
        ),
    ]


def case_4_dates(pairing: Pairing, golden: list[GoldenAction], by_id: dict, extractions: list) -> list[Metric]:
    """An invented date is a concrete date the transcript did not support.

    Two ways it can happen, both counted:
      * the golden label says no date was stated, and a concrete one came back
      * a concrete date came back with no record of the words that produced it
    """
    invented: list[str] = []

    for item in golden:
        extraction_id = pairing.matched.get(item.id)
        if extraction_id is None:
            continue
        payload = by_id[extraction_id].payload
        produced = payload.get("due_date", UNSPECIFIED)
        if item.due_date == UNSPECIFIED and produced != UNSPECIFIED:
            invented.append(f"{item.id}: no date stated, but {produced!r} was returned")

    for extraction in extractions:
        payload = extraction.payload
        if payload.get("due_date", UNSPECIFIED) != UNSPECIFIED and not payload.get("due_date_stated"):
            invented.append(f"{extraction.id}: concrete date with no stated source text")

    resolved = [
        e for e in extractions if e.payload.get("due_date_type") == "relative_resolved"
    ]
    metrics = [
        Metric(
            case="4",
            name="Invented dates",
            measured=str(len(invented)),
            target="= 0",
            passed=len(invented) == 0,
            detail="; ".join(invented[:3]) if invented else "no date was produced without support",
        )
    ]
    if resolved:
        example = resolved[0].payload
        metrics.append(
            Metric(
                case="4b",
                name="Relative dates resolved",
                measured=str(len(resolved)),
                target="reported",
                detail=f"e.g. {example.get('due_date_stated')!r} -> {example.get('due_date')}, "
                f"rule: {(example.get('due_date_rule') or '')[:80]}",
            )
        )
    return metrics


#: Words a quote must contain for a "high" severity to be readable from it.
#: Not a check on whether the severity is right, but on whether somebody
#: holding only the quote could see why that level was chosen, which is what
#: M5's capability test actually asks.
_CONSEQUENCE_WORDS = (
    "miss", "missed", "delay", "postpone", "lose", "lost", "trust", "deadline",
    "blocked", "block", "cannot", "can't", "fail", "risk", "grey", "legal",
)


def case_5_decisions(decided, deferred, extractions) -> list[Metric]:
    """Golden case 5, and the decision recall that surrounds it.

    The negative test comes first because it is the one the brief singles out:
    a system that finds every decision and also records the deferral has not
    passed. It has recorded something that never happened, and the reviewer
    cannot tell, because the quote will be perfectly genuine.
    """
    recorded = [d for d in deferred if quote_present(d.verbatim_quote, extractions)]
    found = [d for d in decided if quote_present(d.verbatim_quote, extractions)]
    recall = len(found) / len(decided) if decided else 0.0

    metrics = [
        Metric(
            case="5",
            name="Deferred items recorded",
            measured=str(len(recorded)),
            target="= 0",
            passed=len(recorded) == 0,
            detail=(
                "; ".join(f"{d.id} was deferred but appears in the decision log" for d in recorded)
                if recorded
                else f"{len(deferred)} proposed-then-deferred item(s) correctly absent"
            ),
        ),
        Metric(
            case="5b",
            name="Decision recall",
            measured=f"{recall:.2f}",
            target=">= 0.70",
            passed=recall >= 0.70 if decided else None,
            detail=f"{len(found)}/{len(decided)} hand-labelled decisions found",
        ),
    ]

    missing = [d.id for d in decided if d not in found]
    if missing:
        metrics[1].detail += f". Missed: {', '.join(missing[:4])}"
    return metrics


def case_m5_risks(golden: list[GoldenRisk], extractions) -> list[Metric]:
    found = [r for r in golden if quote_present(r.verbatim_quote, extractions)]
    recall = len(found) / len(golden) if golden else 0.0

    high = [e for e in extractions if e.payload.get("severity") == "high"]
    indefensible = [
        e for e in high
        if not any(word in e.verbatim_quote.lower() for word in _CONSEQUENCE_WORDS)
    ]

    return [
        Metric(
            case="M5",
            name="Risk recall",
            measured=f"{recall:.2f}",
            target=">= 0.70",
            passed=recall >= 0.70 if golden else None,
            detail=f"{len(found)}/{len(golden)} hand-labelled risks found",
        ),
        Metric(
            case="M5b",
            name="Severity defensible",
            measured=f"{len(high) - len(indefensible)}/{len(high)}" if high else "0/0",
            target="all",
            passed=not indefensible if high else None,
            detail=(
                "; ".join(f"{e.id} is high but its quote states no consequence" for e in indefensible[:2])
                if indefensible
                else "every high-severity quote states the consequence it rests on"
            ),
        ),
    ]


def case_6_retrieval(settings: Settings, run_model: bool) -> list[Metric]:
    """Golden case 6, and the mode comparison the brief's warning deserves.

    "All five golden questions return the correct source in the top three
     results. The one golden question whose answer is genuinely absent from the
     corpus returns a not-found response rather than a plausible fabrication."

    The second half needs the model. The mode comparison does not, so it runs
    regardless: retrieval is free and the whole point of choosing hybrid over
    keyword is a claim that should be measured rather than asserted.
    """
    questions = load_questions()
    answerable = [q for q in questions if q.answerable]
    unanswerable = [q for q in questions if not q.answerable]
    metrics: list[Metric] = []

    # --- 6c: the comparison, retrieval only, no model calls -----------------
    embedder = get_embedder(settings)
    per_mode: dict[str, tuple[int, int, float]] = {}

    with database.connect(settings) as conn:
        expected_segments = {}
        for question in answerable:
            row = conn.execute(
                "SELECT id FROM segments WHERE source_id = ? AND start_ts = ?",
                (question.expected_source_id, question.expected_timestamp),
            ).fetchone()
            expected_segments[question.id] = row["id"] if row else None

    for mode in ("keyword", "dense", "hybrid"):
        source_hits = segment_hits = 0
        ranks: list[int] = []
        for question in answerable:
            # neighbours=0: expansion would flatter every mode equally and
            # hide which one actually found the cited segment.
            found = search(question.question, settings, mode=mode, limit=10, neighbours=0)
            top3 = found[:3]
            source_hits += any(h.source_id == question.expected_source_id for h in top3)

            wanted = expected_segments.get(question.id)
            position = next((i for i, h in enumerate(found, 1) if h.ref_id == wanted), None)
            segment_hits += int(position is not None and position <= 3)
            ranks.append(position or 99)
        per_mode[mode] = (source_hits, segment_hits, sum(ranks) / len(ranks) if ranks else 0.0)

    configured = settings.retrieval_mode
    source_hits, segment_hits, mean_rank = per_mode[configured]

    metrics.append(
        Metric(
            case="6",
            name="Retrieval, correct source",
            measured=f"{source_hits}/{len(answerable)}",
            target=f"{len(answerable)}/{len(answerable)}",
            passed=source_hits == len(answerable),
            detail=f"correct source in the top three, mode={configured}",
        )
    )
    metrics.append(
        Metric(
            case="6c",
            name="Mode comparison",
            measured=configured,
            target="reported",
            detail=" | ".join(
                f"{mode}: source {s}/{len(answerable)}, segment {g}/{len(answerable)}, mean rank {r:.1f}"
                for mode, (s, g, r) in per_mode.items()
            )
            + f" [embedder: {embedder.name}]",
        )
    )

    # --- 6b: the not-found path, which does need the model ------------------
    if run_model and unanswerable:
        refused = 0
        detail = []
        for question in unanswerable:
            answer = answer_question(question.question, settings)
            refused += int(not answer.found)
            if answer.found:
                detail.append(f"{question.id} was answered when it should not have been")
        metrics.append(
            Metric(
                case="6b",
                name="Not-found on the unanswerable",
                measured=f"{refused}/{len(unanswerable)}",
                target="all",
                passed=refused == len(unanswerable),
                detail="; ".join(detail)
                or "the question with no answer in the corpus was correctly refused",
            )
        )

        answered = cited = 0
        for question in answerable:
            answer = answer_question(question.question, settings)
            answered += int(answer.found)
            cited += int(bool(answer.claims))
        metrics.append(
            Metric(
                case="6d",
                name="Answers carrying a verified citation",
                measured=f"{cited}/{len(answerable)}",
                target="all",
                passed=cited == len(answerable),
                detail=f"{answered} answered, {cited} with at least one citation that verified "
                f"against the source it cites",
            )
        )

    return metrics


def case_7_signals(settings: Settings) -> list[Metric]:
    """Golden case 7.

    "Precision on the golden-labelled subset is at least 0.7 and the two
     direct-message records in the export are excluded from processing
     entirely."

    Precision rather than accuracy, because the cost is asymmetric: every
    non-noise label becomes something a human has to review, so a greeting
    labelled a request wastes a person's attention while a borderline request
    labelled noise merely loses a little signal.
    """
    labelled, forbidden_ids = load_signals()
    metrics: list[Metric] = []

    with database.connect(settings) as conn:
        stored = {
            m.id: m for m in chat_repo.list_messages(conn)
        }
        total_messages = len(stored)
        leaked = [mid for mid in forbidden_ids if mid in stored]
        dm_rows = conn.execute(
            "SELECT COUNT(*) AS n FROM chat_messages WHERE is_direct_message = 1"
        ).fetchone()["n"]

    # --- 7b: the direct messages, asserted by absence -----------------------
    metrics.append(
        Metric(
            case="7b",
            name="Direct messages in the store",
            measured=str(len(leaked) + dm_rows),
            target="= 0",
            passed=(not leaked) and dm_rows == 0 and total_messages > 0,
            detail=(
                f"{len(forbidden_ids)} DM ids checked against {total_messages} stored messages"
                if total_messages
                else "NOTHING is stored, so zero DMs proves nothing. Ingest the export first."
            ),
        )
    )

    if not any(m.classification for m in stored.values()):
        metrics.append(
            Metric(
                case="7",
                name="Chat signal precision",
                measured="-",
                target=">= 0.70",
                detail="no message has been classified yet",
            )
        )
        return metrics

    # --- 7 and 7c: precision, overall and per class -------------------------
    # A message absent from the store was classified noise and discarded, which
    # is a prediction of noise and has to be counted as one.
    predicted = {
        item.message_id: (
            stored[item.message_id].classification.value
            if item.message_id in stored and stored[item.message_id].classification
            else "noise"
        )
        for item in labelled
    }
    truth = {item.message_id: item.classification for item in labelled}

    classes = sorted({*truth.values(), *predicted.values()})
    per_class: list[str] = []
    non_noise_correct = non_noise_predicted = 0

    for label in classes:
        tp = sum(1 for k in truth if predicted[k] == label and truth[k] == label)
        fp = sum(1 for k in truth if predicted[k] == label and truth[k] != label)
        fn = sum(1 for k in truth if predicted[k] != label and truth[k] == label)
        precision = tp / (tp + fp) if (tp + fp) else None
        recall = tp / (tp + fn) if (tp + fn) else None
        per_class.append(
            f"{label}: P {precision:.2f} R {recall:.2f} ({tp}/{tp + fp})"
            if precision is not None and recall is not None
            else f"{label}: none predicted"
        )
        if label != "noise":
            non_noise_correct += tp
            non_noise_predicted += tp + fp

    precision = non_noise_correct / non_noise_predicted if non_noise_predicted else 0.0
    exact = sum(1 for k in truth if predicted[k] == truth[k])

    metrics.append(
        Metric(
            case="7",
            name="Chat signal precision",
            measured=f"{precision:.2f}",
            target=">= 0.70",
            passed=precision >= 0.70 if non_noise_predicted else None,
            detail=f"{non_noise_correct}/{non_noise_predicted} non-noise labels correct, "
            f"{exact}/{len(truth)} of the labelled subset matched exactly",
        )
    )
    metrics.append(
        Metric(case="7c", name="Per class", measured="", target="reported", detail=" | ".join(per_class))
    )
    return metrics


def calibration(pairing: Pairing, extractions: list, golden_count: int) -> list[dict]:
    """Does the model's own confidence predict whether it was right?

    A stretch item the brief lists, obtained almost free once the pairing
    exists. If precision does not rise with the threshold, the confidence value
    is decoration and the README should say so.
    """
    matched_ids = set(pairing.matched.values())
    rows = []
    for threshold in CONFIDENCE_THRESHOLDS:
        kept = [e for e in extractions if (e.confidence or 0.0) >= threshold]
        correct = [e for e in kept if e.id in matched_ids]
        rows.append(
            {
                "threshold": threshold,
                "kept": len(kept),
                "correct": len(correct),
                "precision": round(len(correct) / len(kept), 3) if kept else None,
                "recall": round(len(correct) / golden_count, 3) if golden_count else None,
            }
        )
    return rows


# =============================================================================
# Running
# =============================================================================


#: Which capabilities a run exercises. Scoping this matters on a free tier:
#: one full run over three capabilities and two transcripts costs eighteen
#: model requests against a daily allowance of twenty.
ALL_CAPABILITIES = ("actions", "decisions", "risks", "qa", "signals")


def _chat_sources(settings: Settings) -> list[str]:
    with database.connect(settings) as conn:
        return [
            r["id"]
            for r in conn.execute(
                "SELECT id FROM sources WHERE source_type = 'chat_export'"
                " AND status = 'ingested' AND consent_flag = 1"
            )
        ]


def run_evaluation(
    settings: Settings | None = None,
    *,
    extract: bool = True,
    capabilities: tuple[str, ...] = ALL_CAPABILITIES,
) -> EvalReport:
    cfg = settings or get_settings()
    provider = get_llm_provider(cfg)
    prompt = load_prompt("extract_actions")

    failed_chunks: list[str] = []
    if extract:
        ingest_from_manifest(cfg)
        extractors = {
            "actions": extract_actions,
            "decisions": extract_decisions,
            "risks": extract_risks,
        }
        for name in capabilities:
            if name not in extractors:
                continue
            for source_id in sorted(SCORED_SOURCES):
                failed_chunks.extend(extractors[name](source_id, cfg).failed_chunks)

        if "signals" in capabilities:
            for source in _chat_sources(cfg):
                failed_chunks.extend(classify_signals(source, cfg).failed_batches)

        # The vector index has to be rebuilt after extraction, because approved
        # extractions are searchable alongside transcript segments.
        rebuild(cfg)

    with database.connect(cfg) as conn:
        def _load(kind: ExtractionType) -> list:
            return [
                e
                for source_id in sorted(SCORED_SOURCES)
                for e in extraction_repo.list_extractions(
                    conn, source_id=source_id, extraction_type=kind
                )
            ]

        extractions = _load(ExtractionType.ACTION)
        decision_rows = _load(ExtractionType.DECISION)
        risk_rows = _load(ExtractionType.RISK)
        source_texts = {s: segment_repo.get_source_text(conn, s) for s in SCORED_SOURCES}
        usage = llm_call_repo.summarise(conn)

    golden = load_actions(SCORED_SOURCES)
    by_id = {e.id: e for e in extractions}
    pairing = pair(golden, extractions)

    # With --score-only nothing was called, so the configured provider is not
    # necessarily the one that produced the stored extractions. Reporting the
    # configured model would attribute a result to a model that never saw the
    # transcript, which is the sort of misattribution this whole harness exists
    # to prevent. Take it from the rows instead.
    scored_models = sorted({e.model_name for e in extractions if e.model_name})
    scored_providers = sorted({e.provider for e in extractions if e.provider})
    if not extract and scored_models:
        provider_name = "+".join(scored_providers) or provider.name
        model_name = "+".join(scored_models)
    else:
        provider_name, model_name = provider.name, provider.model

    scored_prompts = sorted({e.prompt_version for e in extractions if e.prompt_version})
    if not extract and scored_prompts:
        prompt_tag = "+".join(scored_prompts)
    else:
        prompt_tag = prompt.version_tag

    report = EvalReport(
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        # A run against the deterministic stub exercises the scoring code. It
        # measures nothing about a model and must never be quoted as a result.
        is_measurement=provider_name != "fake",
        provider=provider_name,
        model=model_name,
        prompt_version=prompt_tag,
        cache_enabled=cfg.llm_cache_enabled,
        sources=sorted(SCORED_SOURCES),
        golden_actions=len(golden),
        extracted_actions=len(extractions),
        incomplete_reason=(
            f"{len(failed_chunks)} of the transcript chunks could not be extracted, "
            f"typically a rate limit or an exhausted free-tier quota. The numbers below "
            f"describe only the chunks that succeeded and measure nothing."
            if failed_chunks
            else None
        ),
        misses=pairing.missed,
        false_positives=pairing.false_positives,
        usage={
            "attempts": usage.attempts,
            "calls": usage.calls,
            "retry_rate": round(usage.retry_rate, 3),
            "cache_hits": usage.cache_hits,
            "prompt_tokens": usage.prompt_tokens,
            "completion_tokens": usage.completion_tokens,
            "total_latency_ms": usage.total_latency_ms,
            "outcomes": usage.outcomes,
        },
    )

    decided, deferred = load_decisions(SCORED_SOURCES)
    golden_risks = load_risks(SCORED_SOURCES)

    report.metrics = [
        *case_1_recall(pairing, golden),
        *case_2_fabricated_quotes(extractions + decision_rows + risk_rows, source_texts),
        *case_3_owner(pairing, golden, by_id),
        *case_4_dates(pairing, golden, by_id, extractions),
    ]
    if "decisions" in capabilities or decision_rows:
        report.metrics += case_5_decisions(decided, deferred, decision_rows)
    if "risks" in capabilities or risk_rows:
        report.metrics += case_m5_risks(golden_risks, risk_rows)
    if VectorIndex(cfg).ready() or cfg.retrieval_mode == "keyword":
        report.metrics += case_6_retrieval(cfg, run_model="qa" in capabilities)
    report.metrics += case_7_signals(cfg)
    report.calibration = calibration(pairing, extractions, len(golden))
    return report


# =============================================================================
# Output
# =============================================================================


def render(report: EvalReport, colour: bool = True) -> str:
    green, red, yellow, dim, bold, off = (
        ("\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m") if colour else ("",) * 6
    )
    out = io.StringIO()
    w = out.write

    w(f"\n{bold}Meeting & Channel Intelligence Agent - evaluation{off}\n")
    if report.incomplete_reason:
        w(f"\n  {red}{bold}INCOMPLETE RUN{off}\n  {yellow}{report.incomplete_reason}{off}\n")
    if not report.is_measurement:
        w(f"\n  {red}{bold}NOT A MEASUREMENT{off}\n"
          f"  {yellow}This run used the deterministic stub provider, which answers from the\n"
          f"  golden file itself. It proves the pipeline and the scoring code work end to\n"
          f"  end. It says nothing whatever about model quality, and these numbers must\n"
          f"  not be quoted in the README or anywhere else as a result.{off}\n")
    w(f"{dim}{report.generated_at}   provider {report.provider}:{report.model}   "
      f"prompt extract_actions v{report.prompt_version}   "
      f"cache {'on' if report.cache_enabled else 'off'}{off}\n")
    w(f"{dim}sources: {', '.join(report.sources)}{off}\n")
    w(f"{dim}{report.golden_actions} hand-labelled actions, {report.extracted_actions} extracted{off}\n\n")

    w(f"  {'':<4}{'Metric':<28}{'Measured':>10}   {'Target':<10} Status\n")
    w(f"  {'-' * 68}\n")
    for metric in report.metrics:
        if metric.passed is None:
            status = f"{dim}reported{off}"
        elif metric.passed:
            status = f"{green}PASS{off}"
        else:
            status = f"{red}FAIL{off}"
        w(f"  {metric.case:<4}{metric.name:<28}{metric.measured:>10}   {metric.target:<10} {status}\n")
        if metric.detail:
            w(f"      {dim}{metric.detail}{off}\n")
    w("\n")

    w(f"  {bold}Confidence calibration{off}  {dim}does the model's own confidence predict correctness{off}\n")
    w(f"  {'threshold':<12}{'kept':>6}{'correct':>9}{'precision':>11}{'recall':>9}\n")
    for row in report.calibration:
        precision = f"{row['precision']:.2f}" if row["precision"] is not None else "-"
        recall = f"{row['recall']:.2f}" if row["recall"] is not None else "-"
        w(f"  >= {row['threshold']:<9.2f}{row['kept']:>6}{row['correct']:>9}{precision:>11}{recall:>9}\n")
    w("\n")

    usage = report.usage
    w(f"  {bold}Model usage{off}  {usage['attempts']} attempt(s) for {usage['calls']} call(s), "
      f"retry rate {usage['retry_rate']:.0%}, {usage['cache_hits']} cache hit(s)\n")
    w(f"  {dim}{usage['prompt_tokens']} prompt + {usage['completion_tokens']} completion tokens, "
      f"{usage['total_latency_ms']} ms total{off}\n")
    w(f"  {dim}outcomes: {usage['outcomes']}{off}\n\n")

    if report.misses:
        w(f"  {yellow}Missed{off} {dim}(in the golden set, not extracted){off}\n")
        for item in report.misses:
            w(f"    {item}\n")
        w("\n")
    if report.false_positives:
        w(f"  {yellow}False positives{off} {dim}(extracted, matching no golden action){off}\n")
        for item in report.false_positives[:10]:
            w(f"    {item}\n")
        w("\n")

    failed = report.failed
    if failed:
        w(f"  {red}{bold}{len(failed)} metric(s) below target{off}: "
          f"{', '.join(m.name for m in failed)}\n\n")
    else:
        w(f"  {green}{bold}every targeted metric met{off}\n\n")
    return out.getvalue()


def _numeric(value: str) -> float | None:
    try:
        return float(value)
    except ValueError:
        if "/" in value:  # "2/2"
            left, _, right = value.partition("/")
            try:
                return float(left) / float(right) if float(right) else 0.0
            except ValueError:
                return None
        return None


def run_repeated(settings: Settings | None = None, runs: int = 3) -> RepeatedReport:
    """Run the whole pipeline `runs` times with the cache bypassed.

    The cache must be off or every run after the first would replay the first,
    which would report perfect stability that does not exist.
    """
    cfg = (settings or get_settings()).model_copy(update={"llm_cache_enabled": False})
    reports = []
    for index in range(runs):
        report = run_evaluation(cfg)
        if report.incomplete_reason:
            report.incomplete_reason = f"run {index + 1} of {runs}: {report.incomplete_reason}"
            reports.append(report)
            break
        reports.append(report)
    first = reports[0]

    ordered: list[str] = []
    by_case: dict[str, MetricRange] = {}

    for report in reports:
        for metric in report.metrics:
            if metric.case not in by_case:
                ordered.append(metric.case)
                by_case[metric.case] = MetricRange(
                    case=metric.case,
                    name=metric.name,
                    target=metric.target,
                    targeted=metric.passed is not None,
                )
            entry = by_case[metric.case]
            entry.values.append(metric.measured)
            number = _numeric(metric.measured)
            if number is not None:
                entry.numeric.append(number)
            if metric.passed is not None:
                entry.total += 1
                entry.passes += int(metric.passed)

    # llm_calls accumulates across runs in the same database, so the last
    # report already holds the total. Summing the reports would count the
    # first run once per subsequent run.
    totals = {k: v for k, v in reports[-1].usage.items() if isinstance(v, (int, float))}

    return RepeatedReport(
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        runs=runs,
        is_measurement=first.is_measurement,
        incomplete_reason=next((r.incomplete_reason for r in reports if r.incomplete_reason), None),
        provider=first.provider,
        model=first.model,
        prompt_version=first.prompt_version,
        sources=first.sources,
        golden_actions=first.golden_actions,
        extracted_per_run=[r.extracted_actions for r in reports],
        metrics=[by_case[case] for case in ordered],
        usage=totals,
    )


def render_repeated(report: RepeatedReport, colour: bool = True) -> str:
    green, red, yellow, dim, bold, off = (
        ("\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m") if colour else ("",) * 6
    )
    out = io.StringIO()
    w = out.write

    w(f"\n{bold}Meeting & Channel Intelligence Agent - evaluation over {report.runs} runs{off}\n")
    if report.incomplete_reason:
        w(f"\n  {red}{bold}INCOMPLETE{off}  {yellow}{report.incomplete_reason}{off}\n")
    if not report.is_measurement:
        w(f"\n  {red}{bold}NOT A MEASUREMENT{off}  {yellow}stub provider{off}\n")
    w(f"{dim}{report.generated_at}   provider {report.provider}:{report.model}   "
      f"prompt extract_actions v{report.prompt_version}   cache bypassed{off}\n")
    w(f"{dim}{report.golden_actions} hand-labelled actions. Extracted per run: "
      f"{', '.join(str(n) for n in report.extracted_per_run)}{off}\n")
    w(f"{dim}The model is not deterministic, so a single run is a sample. A target counts as\n"
      f"met only when every run met it.{off}\n\n")

    w(f"  {'':<4}{'Metric':<28}{'Worst':>8}{'Range':>14}   {'Target':<10} Status\n")
    w(f"  {'-' * 76}\n")
    for metric in report.metrics:
        if metric.passed is None:
            status = f"{dim}reported{off}"
        elif metric.passed:
            status = f"{green}PASS{off}" + ("" if metric.stable else f" {yellow}(varies){off}")
        else:
            status = f"{red}FAIL{off} {dim}{metric.passes}/{metric.total} runs{off}"
        w(f"  {metric.case:<4}{metric.name:<28}{metric.worst:>8}{metric.spread:>14}   "
          f"{metric.target:<10} {status}\n")
    w("\n")

    usage = report.usage
    w(f"  {bold}Model usage across {report.runs} runs{off}  {int(usage.get('attempts', 0))} attempt(s), "
      f"{int(usage.get('prompt_tokens', 0))} prompt + {int(usage.get('completion_tokens', 0))} "
      f"completion tokens, {int(usage.get('total_latency_ms', 0))} ms\n\n")

    unstable = report.unstable
    if unstable:
        w(f"  {yellow}{bold}{len(unstable)} targeted metric(s) varied between runs{off}: "
          f"{', '.join(m.name for m in unstable)}\n")
        w(f"  {dim}This is the model, not the harness. Reported rather than smoothed away.{off}\n\n")

    failed = report.failed
    if failed:
        w(f"  {red}{bold}{len(failed)} metric(s) failed in at least one run{off}: "
          f"{', '.join(m.name for m in failed)}\n\n")
    else:
        w(f"  {green}{bold}every targeted metric met in every run{off}\n\n")
    return out.getvalue()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--score-only", action="store_true", help="score what is already stored")
    parser.add_argument("--no-cache", action="store_true", help="bypass the response cache")
    parser.add_argument("--provider", choices=("gemini", "ollama", "fake"))
    parser.add_argument("--runs", type=int, default=1,
                        help="repeat the whole pipeline N times and report the range")
    parser.add_argument("--capabilities", default=",".join(ALL_CAPABILITIES),
                        help="comma-separated: actions, decisions, risks. One full run over "
                             "all three costs eighteen model requests against a free-tier "
                             "allowance of twenty a day.")
    parser.add_argument("--sources", default=None,
                        help="comma-separated source ids to score instead of the committed "
                             "corpus. Results are printed but never written, so scoring a "
                             "different corpus cannot overwrite the committed baseline.")
    parser.add_argument("--out", default=str(REPO_ROOT / "eval" / "results.txt"))
    args = parser.parse_args()

    overrides: dict[str, object] = {}
    if args.no_cache:
        overrides["llm_cache_enabled"] = False
    if args.provider:
        overrides["llm_provider"] = args.provider
    settings = get_settings().model_copy(update=overrides)

    scoring_other_sources = False
    if args.sources:
        global SCORED_SOURCES
        SCORED_SOURCES = {s.strip() for s in args.sources.split(",") if s.strip()}
        scoring_other_sources = True

    provider = get_llm_provider(settings)
    usable, reason = provider.available()
    if not usable and not args.score_only:
        print(f"\ncannot run: {reason}\n"
              f"Set a provider in .env, or pass --score-only to score what is already stored.\n")
        return 1

    if args.runs > 1:
        repeated = run_repeated(settings, args.runs)
        print(render_repeated(repeated, colour=sys.stdout.isatty()))
        if repeated.incomplete_reason:
            print(f"results not written: {repeated.incomplete_reason}\n"
                  f"The committed eval/results.txt is left untouched.\n")
            return 2
        if not repeated.is_measurement:
            print("results not written: a stub run is not a measurement\n")
            return 0
        Path(args.out).write_text(render_repeated(repeated, colour=False), encoding="utf-8")
        Path(args.out).with_suffix(".json").write_text(
            repeated.model_dump_json(indent=2), encoding="utf-8"
        )
        print(f"written to {args.out}\n")
        return 1 if repeated.failed else 0

    chosen = tuple(c.strip() for c in args.capabilities.split(",") if c.strip())
    unknown = set(chosen) - set(ALL_CAPABILITIES)
    if unknown:
        print(f"unknown capability: {', '.join(sorted(unknown))}. "
              f"Choose from {', '.join(ALL_CAPABILITIES)}.")
        return 1

    report = run_evaluation(settings, extract=not args.score_only, capabilities=chosen)
    print(render(report, colour=sys.stdout.isatty()))

    if scoring_other_sources:
        print(f"results not written: --sources was given, so this scored "
              f"{', '.join(sorted(SCORED_SOURCES))} rather than the committed corpus. "
              f"eval/results.txt describes a fixed pair of transcripts and stays that way.\n")
        return 1 if report.failed else 0

    if report.incomplete_reason:
        print(f"results not written: {report.incomplete_reason}\n"
              f"The committed eval/results.txt is left untouched. Cached responses make a\n"
              f"re-run free once the quota resets; `make eval` will use them.\n")
        return 2

    if not report.is_measurement:
        print("results not written: a stub run is not a measurement and must not be "
              "committed as one\n")
        return 0

    Path(args.out).write_text(render(report, colour=False), encoding="utf-8")
    Path(args.out).with_suffix(".json").write_text(report.model_dump_json(indent=2), encoding="utf-8")
    print(f"written to {args.out} and {Path(args.out).with_suffix('.json').name}\n")

    return 1 if report.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
