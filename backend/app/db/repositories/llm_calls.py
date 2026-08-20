"""Reads and writes over `llm_calls`."""

from __future__ import annotations

import sqlite3

from app.models.telemetry import CallOutcome, LLMCall, UsageSummary

_COLUMNS = (
    "id, call_id, source_id, capability, provider, model, prompt_version, attempt, outcome, "
    "prompt_tokens, completion_tokens, latency_ms, cache_hit, error, created_at"
)


def record_call(conn: sqlite3.Connection, call: LLMCall) -> None:
    conn.execute(
        f"INSERT INTO llm_calls ({_COLUMNS}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            call.id,
            call.call_id,
            call.source_id,
            call.capability,
            call.provider,
            call.model,
            call.prompt_version,
            call.attempt,
            call.outcome.value,
            call.prompt_tokens,
            call.completion_tokens,
            call.latency_ms,
            int(call.cache_hit),
            call.error,
            call.created_at,
        ),
    )


def summarise(conn: sqlite3.Connection, source_id: str | None = None, capability: str | None = None) -> UsageSummary:
    clauses, params = [], []
    if source_id:
        clauses.append("source_id = ?")
        params.append(source_id)
    if capability:
        clauses.append("capability = ?")
        params.append(capability)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""

    row = conn.execute(
        "SELECT COUNT(*) AS attempts,"
        " COUNT(DISTINCT call_id) AS calls,"
        " COALESCE(SUM(cache_hit), 0) AS cache_hits,"
        " COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens,"
        " COALESCE(SUM(completion_tokens), 0) AS completion_tokens,"
        f" COALESCE(SUM(latency_ms), 0) AS latency_ms FROM llm_calls{where}",
        tuple(params),
    ).fetchone()

    outcomes = {
        r["outcome"]: r["n"]
        for r in conn.execute(
            f"SELECT outcome, COUNT(*) AS n FROM llm_calls{where} GROUP BY outcome", tuple(params)
        ).fetchall()
    }

    return UsageSummary(
        calls=int(row["calls"]),
        attempts=int(row["attempts"]),
        cache_hits=int(row["cache_hits"]),
        prompt_tokens=int(row["prompt_tokens"]),
        completion_tokens=int(row["completion_tokens"]),
        total_latency_ms=int(row["latency_ms"]),
        outcomes=outcomes,
    )


def recent(conn: sqlite3.Connection, limit: int = 50) -> list[LLMCall]:
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM llm_calls ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()
    return [
        LLMCall(
            id=r["id"],
            call_id=r["call_id"],
            source_id=r["source_id"],
            capability=r["capability"],
            provider=r["provider"],
            model=r["model"],
            prompt_version=r["prompt_version"],
            attempt=r["attempt"],
            outcome=CallOutcome(r["outcome"]),
            prompt_tokens=r["prompt_tokens"],
            completion_tokens=r["completion_tokens"],
            latency_ms=r["latency_ms"],
            cache_hit=bool(r["cache_hit"]),
            error=r["error"],
            created_at=r["created_at"],
        )
        for r in rows
    ]
