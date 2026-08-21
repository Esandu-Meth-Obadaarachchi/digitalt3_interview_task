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
def scripted_model(golden_actions):
    """A provider that answers a chunk with the golden actions it contains.

    Simulates a model that gets everything right, which is what makes the
    pipeline and the scoring code testable without a network. Tests that need a
    model to get something *wrong* script the failure explicitly instead.
    """
    import json

    from app.extraction.llm.factory import set_provider_override
    from app.extraction.llm.fake import FakeProvider
    from app.ingestion.normaliser import normalise_text

    installed: list = []

    def install(transform=None):
        def respond(request):
            chunk = normalise_text(request.prompt)
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
        installed.append(provider)
        return provider

    yield install
    set_provider_override(None)
