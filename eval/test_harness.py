"""Tests for the harness's own scoring.

A harness that scores wrongly is worse than no harness, because its numbers are
believed. These tests pin the matching rule and every metric against inputs
whose correct answer is known by construction.
"""

from __future__ import annotations

import json

import pytest
from golden import GoldenAction, load_actions, matches, pair, quotes_overlap
import harness
from harness import SCORED_SOURCES, render, run_evaluation

from app.db import database
from app.db.repositories import extractions as extraction_repo
from app.extraction.llm.factory import set_provider_override
from app.extraction.llm.fake import FakeProvider
from app.models.common import UNSPECIFIED, ExtractionType


# --- the golden set itself ---------------------------------------------------


def test_the_golden_set_covers_both_valid_transcripts():
    golden = load_actions(SCORED_SOURCES)
    assert len(golden) == 13
    assert {g.source_id for g in golden} == SCORED_SOURCES


def test_the_golden_set_plants_the_difficulties_the_brief_asks_for():
    golden = load_actions(SCORED_SOURCES)

    ownerless = [g for g in golden if g.owner == UNSPECIFIED]
    assert len(ownerless) == 2, "at least two commitments with no owner stated"

    relative = [g for g in golden if g.due_date_type == "relative"]
    assert relative, "at least one commitment with a relative date only"


# --- the matching rule -------------------------------------------------------


def test_quote_overlap_matches_in_either_direction():
    assert quotes_overlap("done with tests by Friday", "I can have the refactor done with tests by Friday")
    assert quotes_overlap("I can have the refactor done with tests by Friday", "done with tests by Friday")
    assert not quotes_overlap("done with tests by Friday", "a completely different sentence")


def test_a_neighbouring_quote_of_the_same_exchange_still_matches_on_task():
    item = GoldenAction(
        id="g1", source_id="s", what="Finish the authentication refactor with integration tests",
        owner="Priya Sharma", due_date="Friday", verbatim_quote="I can have the refactor done with tests by Friday",
        speaker="Priya Sharma", timestamp="00:02:17",
    )
    assert matches(item, "a different span entirely", "Complete the authentication refactor and its tests")


def test_pairing_is_one_to_one_so_one_vague_item_cannot_claim_two_commitments():
    from app.models.extraction import Extraction

    golden = [
        GoldenAction(id="g1", source_id="s", what="Write the tests", owner="A", due_date=UNSPECIFIED,
                     verbatim_quote="I will write the tests", speaker="A", timestamp="00:00:01"),
        GoldenAction(id="g2", source_id="s", what="Write the tests", owner="B", due_date=UNSPECIFIED,
                     verbatim_quote="I will write the tests", speaker="B", timestamp="00:05:00"),
    ]
    single = Extraction(
        id="e1", source_id="s", extraction_type=ExtractionType.ACTION,
        payload={"what": "Write the tests"}, original_payload={"what": "Write the tests"},
        verbatim_quote="I will write the tests", created_at="2024-01-01",
    )

    result = pair(golden, [single])
    assert len(result.matched) == 1
    assert result.missed == ["g2"]


def test_an_extraction_matching_nothing_is_a_false_positive():
    from app.models.extraction import Extraction

    golden = [GoldenAction(id="g1", source_id="s", what="Write the tests", owner="A",
                           due_date=UNSPECIFIED, verbatim_quote="I will write the tests",
                           speaker="A", timestamp="00:00:01")]
    noise = Extraction(
        id="e1", source_id="s", extraction_type=ExtractionType.ACTION,
        payload={"what": "Book a meeting room for Thursday"},
        original_payload={"what": "Book a meeting room for Thursday"},
        verbatim_quote="shall we get a room", created_at="2024-01-01",
    )

    result = pair(golden, [noise])
    assert result.false_positives == ["e1"]
    assert result.recall == 0.0
    assert result.precision == 0.0


# --- the metrics -------------------------------------------------------------


#: The stub can stand in for extraction. It cannot stand in for judgement, so
#: the QA cases are scoped out of the fixture the scoring tests use. See
#: test_the_stub_cannot_pass_the_not_found_case for why that is not a gap.
STUB_CAPABILITIES = ("actions", "decisions", "risks")


@pytest.fixture()
def scored(settings, scripted_model):
    scripted_model()
    return run_evaluation(settings, capabilities=STUB_CAPABILITIES)


def _metric(report, case):
    return next(m for m in report.metrics if m.case == case)


def test_a_perfect_model_scores_perfectly(scored):
    """Proves the scoring code, not the model. The provider answers from the
    golden file, so anything short of 1.00 here is a scoring bug."""
    assert _metric(scored, "1").measured == "1.00"
    assert _metric(scored, "2").measured == "0"
    assert _metric(scored, "3a").measured == "1.00"
    assert _metric(scored, "3b").measured == "2/2"
    assert _metric(scored, "4").measured == "0"
    assert scored.failed == []


def test_a_stub_run_is_marked_as_not_a_measurement(scored):
    """It must be impossible to mistake a plumbing check for a result."""
    assert scored.is_measurement is False
    assert "NOT A MEASUREMENT" in render(scored, colour=False)


def test_the_report_names_the_prompt_version_that_produced_it(scored):
    """The shape, not a pinned number: prompts are expected to be revised, and
    a test that fails on every revision teaches nobody anything."""
    import re

    assert re.fullmatch(r"\d+\+[0-9a-f]{6}", scored.prompt_version)
    assert scored.provider == "fake"


def test_the_stub_cannot_pass_the_not_found_case(settings, scripted_model):
    """The boundary of what a test double can prove, asserted rather than left
    implicit.

    The stub answers a question by lexical overlap with the retrieved sources.
    That is enough to exercise the retrieval, the citation verification and the
    scoring, and it is not enough to tell "what database did we decide to use
    for the user analytics module", which the corpus does not answer, from
    "what database", which it does. Two content words overlap and the stub
    answers.

    Golden case 6b measures exactly that judgement, so it needs a real model,
    and the numbers for it in the README come from one. Pinning this here stops
    someone later "fixing" the stub until it passes and believing 6b is covered
    by the suite.
    """
    scripted_model()
    report = run_evaluation(settings, capabilities=("qa",))

    not_found = next(m for m in report.metrics if m.case == "6b")
    assert not_found.passed is False
    assert "should not have been" in not_found.detail


def test_the_mode_comparison_runs_without_a_model(settings, scripted_model):
    """Case 6c is retrieval only, so it costs nothing and always reports."""
    scripted_model()
    report = run_evaluation(settings, capabilities=STUB_CAPABILITIES)

    comparison = next(m for m in report.metrics if m.case == "6c")
    for mode in ("keyword", "dense", "hybrid"):
        assert mode in comparison.detail
    assert "hashing" in comparison.detail, "the report must name the embedder that produced it"


def test_recall_falls_when_the_model_misses_things(settings, scripted_model):
    """Half the actions dropped should halve recall, not merely dent it."""
    scripted_model(transform=lambda actions: actions[:1])
    report = run_evaluation(settings, capabilities=STUB_CAPABILITIES)

    recall = float(_metric(report, "1").measured)
    assert 0.0 < recall < 0.7
    assert _metric(report, "1").passed is False
    assert report.misses


def test_a_guessed_owner_fails_the_unspecified_case(settings, scripted_model):
    """Golden case 3b. A guessed owner is a failure, not a near-miss."""
    def guess_owners(actions):
        return [a | {"owner": "Sarah Chen"} if a["owner"] == UNSPECIFIED else a for a in actions]

    scripted_model(transform=guess_owners)
    report = run_evaluation(settings, capabilities=STUB_CAPABILITIES)

    metric = _metric(report, "3b")
    assert metric.passed is False
    assert metric.measured == "0/2"
    assert "no owner stated, but 'Sarah Chen' was returned" in metric.detail


def test_an_invented_date_fails_the_date_case(settings, scripted_model):
    def invent_dates(actions):
        return [a | {"due_date": "next Tuesday"} if a["due_date"] == UNSPECIFIED else a for a in actions]

    scripted_model(transform=invent_dates)
    report = run_evaluation(settings, capabilities=STUB_CAPABILITIES)

    metric = _metric(report, "4")
    assert metric.passed is False
    assert int(metric.measured) > 0


def test_a_fabricated_quote_is_counted_even_though_it_was_flagged(settings, scripted_model):
    """The count is recomputed from the stored quote and the stored transcript,
    so it does not depend on the code path that set quote_verified."""
    def fabricate(actions):
        return [a | {"verbatim_quote": "words never spoken in this meeting"} for a in actions]

    scripted_model(transform=fabricate)
    report = run_evaluation(settings, capabilities=STUB_CAPABILITIES)

    assert _metric(report, "2").passed is False
    assert int(_metric(report, "2").measured) > 0


def test_extra_items_show_up_as_false_positives_not_as_recall(settings, scripted_model):
    def add_noise(actions):
        return actions + [{
            "what": "Book the meeting room for Thursday", "owner": "Sarah Chen",
            "due_date": UNSPECIFIED, "verbatim_quote": "Alright, let's get started with sprint planning",
            "speaker": "Sarah Chen", "timestamp": "00:00:05", "confidence": 0.4,
        }]

    scripted_model(transform=add_noise)
    report = run_evaluation(settings, capabilities=STUB_CAPABILITIES)

    assert report.false_positives
    assert float(_metric(report, "1b").measured) < 1.0
    assert _metric(report, "1").measured == "1.00", "recall is unaffected by extra items"


def test_calibration_reports_precision_at_each_threshold(scored):
    thresholds = [row["threshold"] for row in scored.calibration]
    assert thresholds == [0.0, 0.5, 0.7, 0.8, 0.9]
    assert all(row["kept"] >= 0 for row in scored.calibration)


def test_usage_statistics_come_from_the_store(scored):
    assert scored.usage["attempts"] > 0
    assert scored.usage["calls"] > 0
    assert "ok" in scored.usage["outcomes"]


def test_the_rendered_report_is_readable_without_colour(scored):
    text = render(scored, colour=False)
    assert "\033[" not in text
    for heading in ("Action recall", "Fabricated quotes", "UNSPECIFIED compliance",
                    "Invented dates", "Confidence calibration", "Model usage"):
        assert heading in text


# --- an incomplete run must never become a committed result ------------------


def test_a_quota_exhausted_run_is_marked_incomplete(settings, monkeypatch):
    """Found the hard way: a three-run evaluation exhausted the Gemini free
    tier's daily cap of 20 requests, every chunk failed, and the harness
    cheerfully scored the empty result and overwrote a good results file with
    meaningless numbers. Committing that would be fabricated evaluation
    results, which is an automatic failure.
    """
    from app.errors import RateLimitedError
    from app.extraction.llm.factory import set_provider_override

    class Exhausted(FakeProvider):
        def generate(self, request):
            raise RateLimitedError("429 RESOURCE_EXHAUSTED: quota exceeded, limit 20 per day")

    set_provider_override(Exhausted())
    try:
        report = run_evaluation(settings)
    finally:
        set_provider_override(None)

    assert report.incomplete_reason is not None
    assert "quota" in report.incomplete_reason
    assert "measure nothing" in report.incomplete_reason
    assert "INCOMPLETE RUN" in render(report, colour=False)


def test_a_transient_rate_limit_is_absorbed_by_the_retry_loop(settings, golden_actions):
    """One 429 is not a failed chunk. The wrapper backs off and retries, which
    is what it is for, so the run stays complete."""
    import json

    from app.errors import RateLimitedError
    from app.extraction.llm.factory import set_provider_override
    from app.ingestion.normaliser import normalise_text

    calls = {"n": 0}

    class FlakyOnce(FakeProvider):
        def generate(self, request):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RateLimitedError("429 RESOURCE_EXHAUSTED")
            chunk = normalise_text(request.prompt)
            return super().generate(request).model_copy(update={"text": json.dumps({"actions": [
                {"what": g["what"], "owner": g["owner"], "due_date": g["due_date"],
                 "verbatim_quote": g["verbatim_quote"], "speaker": g["speaker"],
                 "timestamp": g["timestamp"], "confidence": 0.85}
                for g in golden_actions if normalise_text(g["verbatim_quote"]) in chunk]})})

    set_provider_override(FlakyOnce())
    try:
        # This provider answers only the action contract, so the run is scoped
        # to actions. The point of the test is the retry loop, not coverage.
        report = run_evaluation(settings, capabilities=("actions",))
    finally:
        set_provider_override(None)

    assert report.incomplete_reason is None
    assert report.usage["outcomes"].get("rate_limited") == 1


def test_one_chunk_failing_outright_makes_the_whole_run_incomplete(settings, golden_actions):
    """Scoring five chunks out of six and reporting it as a recall figure would
    understate recall for a reason that has nothing to do with extraction."""
    import json

    from app.errors import RateLimitedError
    from app.extraction.llm.factory import set_provider_override
    from app.ingestion.normaliser import normalise_text

    class OneChunkAlwaysFails(FakeProvider):
        def generate(self, request):
            if "This chunk  : 2 of" in request.prompt:
                raise RateLimitedError("429 RESOURCE_EXHAUSTED: quota exceeded")
            chunk = normalise_text(request.prompt)
            return super().generate(request).model_copy(update={"text": json.dumps({"actions": [
                {"what": g["what"], "owner": g["owner"], "due_date": g["due_date"],
                 "verbatim_quote": g["verbatim_quote"], "speaker": g["speaker"],
                 "timestamp": g["timestamp"], "confidence": 0.85}
                for g in golden_actions if normalise_text(g["verbatim_quote"]) in chunk]})})

    set_provider_override(OneChunkAlwaysFails())
    try:
        report = run_evaluation(settings, capabilities=("actions",))
    finally:
        set_provider_override(None)

    assert report.incomplete_reason is not None
    assert "could not be extracted" in report.incomplete_reason


def test_a_complete_run_is_not_marked_incomplete(scored):
    assert scored.incomplete_reason is None
    assert "INCOMPLETE" not in render(scored, colour=False)


def test_score_only_reports_the_model_that_produced_the_rows(settings, scripted_model):
    """With --score-only nothing is called, so the configured provider is not
    necessarily the one whose output is being scored. Reporting the configured
    model would attribute a result to a model that never saw the transcript."""
    from app.db import database
    from app.extraction.llm.factory import set_provider_override

    scripted_model()
    run_evaluation(settings, capabilities=STUB_CAPABILITIES)

    with database.transaction(settings) as conn:
        conn.execute("UPDATE extractions SET provider = 'gemini', model_name = 'some-other-model'")

    set_provider_override(None)
    report = run_evaluation(settings, extract=False)

    assert report.model == "some-other-model"
    assert report.provider == "gemini"
    assert report.is_measurement is True


# --- the guard that the committed results file needed --------------------------


def test_a_subset_run_prints_its_numbers_and_writes_nothing(settings, scripted_model, tmp_path, monkeypatch):
    """A `--capabilities signals` run once wrote a results file reporting action
    recall 0.00 for a build measuring 0.92, and it stood in the repository for
    six phases contradicting the README.

    --sources already refused for this reason. This is the same rule on the
    other axis.
    """
    import sys

    from app.ingestion.service import ingest_from_manifest

    ingest_from_manifest(settings)
    scripted_model()

    out = tmp_path / "results.txt"
    out.write_text("the committed run, which must survive", encoding="utf-8")

    monkeypatch.setattr(
        sys, "argv",
        ["harness", "--provider", "fake", "--capabilities", "signals", "--out", str(out)],
    )
    harness.main()

    assert out.read_text(encoding="utf-8") == "the committed run, which must survive"
    assert not out.with_suffix(".json").exists()


def test_a_full_run_still_writes(settings, scripted_model, tmp_path, monkeypatch):
    """The guard must not refuse the run it exists to protect."""
    import sys

    from app.ingestion.service import ingest_from_manifest

    ingest_from_manifest(settings)
    scripted_model()

    out = tmp_path / "results.txt"
    monkeypatch.setattr(sys, "argv", ["harness", "--provider", "fake", "--out", str(out)])
    harness.main()

    # The stub refuses for a different reason, which is the point: a fake run
    # is not a measurement either. Both guards hold at once.
    assert not out.exists()
