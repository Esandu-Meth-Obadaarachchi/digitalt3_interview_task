"""Shared test fixtures.

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
