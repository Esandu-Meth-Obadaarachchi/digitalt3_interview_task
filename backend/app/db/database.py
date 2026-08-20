"""SQLite connection management.

Raw sqlite3 rather than an ORM, deliberately:

  * schema.sql is then literally the schema a reviewer reads, which is what
    the toolkit tab asks for ("the schema must be visible in the repo").
  * FTS5 virtual tables, their sync triggers and the consent/approval triggers
    are native SQL. Through an ORM they would need a hand-written migration
    anyway, so the ORM would buy nothing and cost an abstraction to explain.
  * Pydantic already provides the typed contracts, so the ORM's main benefit
    is duplicated.

Connections are short-lived and per-operation. WAL mode allows concurrent
readers alongside a writer, which is all a single-node review tool needs.
PRAGMA foreign_keys is per-connection in SQLite and easy to forget, so it is
set in one place here.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from app.config import Settings, get_settings
from app.errors import translate_sqlite_error


def _configure(conn: sqlite3.Connection) -> sqlite3.Connection:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


@contextmanager
def connect(settings: Settings | None = None) -> Iterator[sqlite3.Connection]:
    """Read-oriented connection. Commits nothing on its own."""
    cfg = settings or get_settings()
    conn = _configure(sqlite3.connect(cfg.db_path, check_same_thread=False))
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction(settings: Settings | None = None) -> Iterator[sqlite3.Connection]:
    """Write connection. Commits on clean exit, rolls back on any exception.

    A database-level refusal (a consent or approval trigger firing) is
    re-raised as the matching domain error, so callers catch
    `ConsentRefused` rather than inspecting a sqlite3 message.
    """
    cfg = settings or get_settings()
    conn = _configure(sqlite3.connect(cfg.db_path, check_same_thread=False))
    try:
        yield conn
        conn.commit()
    except sqlite3.Error as exc:
        conn.rollback()
        raise translate_sqlite_error(exc) from exc
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def schema_sql(settings: Settings | None = None) -> str:
    cfg = settings or get_settings()
    return Path(cfg.schema_path).read_text(encoding="utf-8")


def init_db(settings: Settings | None = None) -> Path:
    """Apply schema.sql to the configured database. Safe to run repeatedly."""
    cfg = settings or get_settings()
    cfg.ensure_directories()
    conn = _configure(sqlite3.connect(cfg.db_path))
    try:
        conn.executescript(schema_sql(cfg))
        conn.commit()
    finally:
        conn.close()
    return cfg.db_path


def reset_db(settings: Settings | None = None) -> Path:
    """Delete the database file and rebuild it from schema.sql.

    The store holds disposable seed data, so `make seed` rebuilding from the
    authoritative schema is simpler and more honest than migration tooling
    that would never be exercised.
    """
    cfg = settings or get_settings()
    for suffix in ("", "-wal", "-shm", "-journal"):
        candidate = Path(str(cfg.db_path) + suffix)
        if candidate.exists():
            candidate.unlink()
    return init_db(cfg)


def fetch_one(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
    return conn.execute(sql, params).fetchone()


def fetch_all(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
    return conn.execute(sql, params).fetchall()


def schema_version(settings: Settings | None = None) -> str | None:
    with connect(settings) as conn:
        row = fetch_one(conn, "SELECT value FROM schema_meta WHERE key = 'schema_version'")
    return row["value"] if row else None
