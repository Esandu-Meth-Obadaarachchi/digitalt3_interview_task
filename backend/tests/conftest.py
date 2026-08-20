"""Shared test fixtures.

Every test runs against a throwaway database built from the real schema.sql,
never against a hand-written test schema. If a constraint is dropped from the
production schema the tests notice.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from app.config import REPO_ROOT, Settings, get_settings
from app.db import database


@pytest.fixture()
def settings(tmp_path: Path) -> Iterator[Settings]:
    """Settings pointed at a temporary database and temporary output paths."""
    get_settings.cache_clear()
    cfg = Settings(
        db_path=tmp_path / "test.db",
        write_log_path=tmp_path / "write_log" / "tracker_writes.jsonl",
        digest_output_dir=tmp_path / "digests",
        outcome_record_dir=tmp_path / "outcomes",
        llm_cache_dir=tmp_path / "llm_cache",
        audio_dir=tmp_path / "audio",
        faiss_index_dir=tmp_path / "faiss",
        sample_data_dir=REPO_ROOT / "sample_data",
        schema_path=REPO_ROOT / "backend" / "app" / "db" / "schema.sql",
        llm_provider="fake",
        llm_cache_enabled=False,
        scheduler_enabled=False,
    )
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
def sample_data_dir() -> Path:
    return REPO_ROOT / "sample_data"
