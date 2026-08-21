"""Shared test fixtures, at the repository root.

At the root rather than under backend/tests/ so that the eval harness's own
tests, which live in eval/, use exactly the same temporary database and the
same scripted model as the unit tests. A harness tested against a different
fixture than the code it scores is not testing much.

Every test runs against a throwaway database built from the real schema.sql,
never against a hand-written test schema. If a constraint is dropped from the
production schema the tests notice.

Configuration is overridden through the environment rather than by passing a
Settings object around, so `get_settings()` returns the test configuration
everywhere, including inside the FastAPI application under test.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.config import REPO_ROOT, Settings, get_settings
from app.db import database

_ENV = {
    "DB_PATH": "test.db",
    "WRITE_LOG_PATH": "write_log/tracker_writes.jsonl",
    "DIGEST_OUTPUT_DIR": "digests",
    "OUTCOME_RECORD_DIR": "outcomes",
    "LLM_CACHE_DIR": "llm_cache",
    "AUDIO_DIR": "audio",
    "FAISS_INDEX_DIR": "faiss",
}


@pytest.fixture()
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Settings]:
    """Settings pointed at a temporary database and temporary output paths."""
    for key, relative in _ENV.items():
        monkeypatch.setenv(key, str(tmp_path / relative))

    monkeypatch.setenv("SAMPLE_DATA_DIR", str(REPO_ROOT / "sample_data"))
    monkeypatch.setenv("SCHEMA_PATH", str(REPO_ROOT / "backend" / "app" / "db" / "schema.sql"))
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    monkeypatch.setenv("LLM_CACHE_ENABLED", "false")
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("LLM_BACKOFF_BASE_SECONDS", "0")
    # Hashing rather than MiniLM: loading a transformer would put seconds on
    # every test that touches retrieval, and the index, the search and the
    # fusion are all exercised for real either way. Retrieval QUALITY is
    # never asserted here, only mechanics; quality is measured by the harness
    # against the real model.
    monkeypatch.setenv("EMBEDDING_PROVIDER", "hashing")

    get_settings.cache_clear()
    cfg = get_settings()
    cfg.ensure_directories()
    database.init_db(cfg)

    yield cfg

    get_settings.cache_clear()


@pytest.fixture()
def conn(settings: Settings) -> Iterator[sqlite3.Connection]:
    """A raw connection to the temporary database, for constraint-level tests."""
    connection = sqlite3.connect(settings.db_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture()
def client(settings: Settings):
    """A FastAPI test client bound to the temporary database.

    Imported lazily so that collecting the test suite does not build the
    application against the developer's real .env.
    """
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def sample_data_dir() -> Path:
    return REPO_ROOT / "sample_data"


@pytest.fixture()
def golden_actions() -> list[dict]:
    import json

    raw = (REPO_ROOT / "sample_data" / "golden" / "golden_actions.json").read_text(encoding="utf-8")
    return json.loads(raw)["actions"]


@pytest.fixture()
def scripted_model(golden_actions, golden_signals, request):
    """A provider that answers whichever contract it is handed.

    Dispatches on the response schema's title, so one fixture serves M3, M4 and
    M5. Simulates a model that gets everything right, which is what makes the
    pipeline and the scoring code testable without a network. Tests that need a
    model to get something *wrong* script the failure explicitly instead.

    `transform` applies to actions only, which is where the deliberate-failure
    tests live.
    """
    import json

    from app.extraction.llm.factory import set_provider_override
    from app.extraction.llm.fake import FakeProvider
    from app.ingestion.normaliser import normalise_text

    golden_dir = REPO_ROOT / "sample_data" / "golden"
    decisions_raw = json.loads((golden_dir / "golden_decisions.json").read_text(encoding="utf-8"))
    risks_raw = json.loads((golden_dir / "golden_risks.json").read_text(encoding="utf-8"))["risks"]

    def install(transform=None, include_deferred: bool = False):
        def respond(request_):
            chunk = normalise_text(request_.prompt)
            title = request_.json_schema.get("title", "")

            if title == "DraftDecisionList":
                items = list(decisions_raw["decisions"])
                if include_deferred:
                    items += [
                        {**d, "what_was_decided": d["what_was_proposed"],
                         "who_stated_it": d.get("who_deferred_it", "UNSPECIFIED")}
                        for d in decisions_raw["deferred_decisions"]
                    ]
                return json.dumps({"decisions": [
                    {
                        "what_was_decided": d["what_was_decided"],
                        "stated_rationale": d.get("stated_rationale", "UNSPECIFIED"),
                        "who_stated_it": d.get("who_stated_it", "UNSPECIFIED"),
                        "alternatives_discussed": d.get("alternatives_discussed", []),
                        "verbatim_quote": d["verbatim_quote"],
                        "timestamp": d["timestamp"],
                        "confidence": 0.9,
                    }
                    for d in items
                    if normalise_text(d["verbatim_quote"]) in chunk
                ]})

            if title == "DraftSignalList":
                # Answers from the hand-labelled subset where it can, and
                # labels everything else noise. Every message sent comes back,
                # which the validator requires.
                import re as _re

                blocks = _re.findall(r"^\[([^\]]+)\] .*?\n\s+(.*?)$", request_.prompt, _re.M)
                labels = {g["message_id"]: g["classification"] for g in golden_signals}
                return json.dumps({"signals": [
                    {
                        "message_id": mid,
                        "classification": labels.get(mid, "noise"),
                        "quote": " ".join(text.split()),
                        "reason": "scripted from the hand-labelled subset",
                        "confidence": 0.8,
                    }
                    for mid, text in blocks
                ]})

            if title == "DraftAnswer":
                # Answering, crudely but honestly. The stub reads the numbered
                # sources out of the rendered prompt and quotes the first one
                # that shares content words with the question. When none does,
                # it says the question cannot be answered, which is what makes
                # the not-found path testable without a real model.
                import re

                blocks = re.findall(
                    r"^\[(\d+)\] meeting:.*?\n\s+speaker:.*?\n\s+text: (.*?)$",
                    request_.prompt, re.M | re.S,
                )
                question = request_.prompt.split("QUESTION", 1)[-1].split("SOURCES", 1)[0]
                stop = {"what", "when", "where", "who", "why", "how", "did", "we", "the", "a",
                        "an", "is", "are", "to", "for", "of", "in", "on", "and", "or", "with",
                        "use", "used", "decide", "our", "that", "this"}
                wanted = {w for w in re.findall(r"[a-z]{3,}", question.lower()) if w not in stop}

                best, best_overlap = None, 0
                for number, text in blocks:
                    overlap = len(wanted & set(re.findall(r"[a-z]{3,}", text.lower())))
                    if overlap > best_overlap:
                        best, best_overlap = (number, text), overlap

                # Two content words in common is a low bar, deliberately: the
                # stub must not be cleverer than the thing it stands in for.
                if best is None or best_overlap < 2:
                    return json.dumps({
                        "answerable": False,
                        "answer": "The sources do not contain the answer to this question.",
                        "claims": [],
                    })

                number, text = best
                sentence = " ".join(text.split())[:160]
                return json.dumps({
                    "answerable": True,
                    "answer": f"Answered from source {number}.",
                    "claims": [{
                        "statement": "The source addresses the question.",
                        "source_index": int(number),
                        "quote": sentence,
                    }],
                })

            if title == "DraftRiskList":
                return json.dumps({"risks": [
                    {
                        "description": r["description"],
                        "severity": r["severity"],
                        "affected_area": r.get("affected_area", "UNSPECIFIED"),
                        "owner": r.get("owner", "UNSPECIFIED"),
                        "verbatim_quote": r["verbatim_quote"],
                        "speaker": r["speaker"],
                        "timestamp": r["timestamp"],
                        "confidence": 0.85,
                    }
                    for r in risks_raw
                    if normalise_text(r["verbatim_quote"]) in chunk
                ]})

            actions = [
                {
                    "what": g["what"],
                    "owner": g["owner"],
                    "due_date": g["due_date"],
                    "verbatim_quote": g["verbatim_quote"],
                    "speaker": g["speaker"],
                    "timestamp": g["timestamp"],
                    "confidence": 0.85,
                }
                for g in golden_actions
                if normalise_text(g["verbatim_quote"]) in chunk
            ]
            if transform is not None:
                actions = transform(actions)
            return json.dumps({"actions": actions})

        provider = FakeProvider().default(respond)
        set_provider_override(provider)
        return provider

    yield install
    set_provider_override(None)


@pytest.fixture()
def golden_decisions() -> dict:
    import json

    raw = (REPO_ROOT / "sample_data" / "golden" / "golden_decisions.json").read_text(encoding="utf-8")
    return json.loads(raw)


@pytest.fixture()
def golden_risks() -> list[dict]:
    import json

    raw = (REPO_ROOT / "sample_data" / "golden" / "golden_risks.json").read_text(encoding="utf-8")
    return json.loads(raw)["risks"]


@pytest.fixture()
def scripted_decision_model(golden_decisions):
    """A provider answering with the golden decisions a chunk contains.

    `include_deferred` scripts the failure golden case 5 exists to catch: a
    model that treats a proposal which was explicitly deferred as a settled
    decision. There is no way to make a live model do that on demand, and a
    negative test that cannot be made to fail proves nothing.
    """
    import json

    from app.extraction.llm.factory import set_provider_override
    from app.extraction.llm.fake import FakeProvider
    from app.ingestion.normaliser import normalise_text

    def install(include_deferred: bool = False):
        items = list(golden_decisions["decisions"])
        if include_deferred:
            items += [
                {
                    "id": d["id"],
                    "source_id": d["source_id"],
                    "what_was_decided": d["what_was_proposed"],
                    "stated_rationale": "UNSPECIFIED",
                    "who_stated_it": d["who_deferred_it"],
                    "alternatives_discussed": [],
                    "verbatim_quote": d["verbatim_quote"],
                    "timestamp": d["timestamp"],
                }
                for d in golden_decisions["deferred_decisions"]
            ]

        def respond(request):
            chunk = normalise_text(request.prompt)
            return json.dumps({"decisions": [
                {
                    "what_was_decided": d["what_was_decided"],
                    "stated_rationale": d.get("stated_rationale", "UNSPECIFIED"),
                    "who_stated_it": d.get("who_stated_it", "UNSPECIFIED"),
                    "alternatives_discussed": d.get("alternatives_discussed", []),
                    "verbatim_quote": d["verbatim_quote"],
                    "timestamp": d["timestamp"],
                    "confidence": 0.9,
                }
                for d in items
                if normalise_text(d["verbatim_quote"]) in chunk
            ]})

        provider = FakeProvider().default(respond)
        set_provider_override(provider)
        return provider

    yield install
    set_provider_override(None)


@pytest.fixture()
def scripted_risk_model(golden_risks):
    """A provider answering with the golden risks a chunk contains."""
    import json

    from app.extraction.llm.factory import set_provider_override
    from app.extraction.llm.fake import FakeProvider
    from app.ingestion.normaliser import normalise_text

    def install(transform=None):
        def respond(request):
            chunk = normalise_text(request.prompt)
            risks = [
                {
                    "description": r["description"],
                    "severity": r["severity"],
                    "affected_area": r.get("affected_area", "UNSPECIFIED"),
                    "owner": r.get("owner", "UNSPECIFIED"),
                    "verbatim_quote": r["verbatim_quote"],
                    "speaker": r["speaker"],
                    "timestamp": r["timestamp"],
                    "confidence": 0.85,
                }
                for r in golden_risks
                if normalise_text(r["verbatim_quote"]) in chunk
            ]
            if transform is not None:
                risks = transform(risks)
            return json.dumps({"risks": risks})

        provider = FakeProvider().default(respond)
        set_provider_override(provider)
        return provider

    yield install
    set_provider_override(None)


@pytest.fixture()
def golden_signals() -> list[dict]:
    import json

    raw = (REPO_ROOT / "sample_data" / "golden" / "golden_signals.json").read_text(encoding="utf-8")
    return json.loads(raw)["labelled_messages"]
