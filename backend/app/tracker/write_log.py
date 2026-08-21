"""The inspectable write log (M7).

The adapter contract requires that mock writes be "persisted and inspectable (a
table, a JSONL log, a file diff) so that during the demo you can show exactly
what the agent would have written to the real system, and prove that nothing
was written without approval".

Two properties matter more than the format:

  every attempt is recorded, including the deduplicated and the blocked ones.
  A log that recorded only successes could not prove a gate ever fired.

  it belongs to the agent, not to the tracker. This is our audit of what we
  tried to do, so it works identically whichever adapter is configured and a
  real integration inherits it for free.

JSONL rather than a table, because during the walkthrough a log can be read
aloud and a table has to be queried.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from app.config import Settings, get_settings
from app.models.tracker import TrackerItemDraft, WriteOutcome

logger = logging.getLogger("agent.tracker")


def append(
    settings: Settings | None,
    extraction_id: str,
    outcome: WriteOutcome,
    provider: str,
    *,
    external_ref: str | None = None,
    reason: str | None = None,
    status: str | None = None,
    draft: TrackerItemDraft | None = None,
) -> None:
    """Append one line. Never raises: the log is evidence, not a dependency."""
    cfg = settings or get_settings()
    record: dict[str, object] = {
        "attempted_at": datetime.now(timezone.utc).isoformat(),
        "extraction_id": extraction_id,
        "outcome": outcome.value,
        "provider": provider,
    }
    if external_ref:
        record["external_ref"] = external_ref
    if reason:
        record["reason"] = reason
    if status:
        record["extraction_status"] = status
    if draft is not None:
        record["payload"] = draft.model_dump()

    try:
        path = Path(cfg.write_log_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
    except OSError:
        logger.warning("could not append to the tracker write log", exc_info=True)


def read(settings: Settings | None = None, limit: int | None = None) -> list[dict]:
    """Read the log back, for the API and the walkthrough."""
    cfg = settings or get_settings()
    path = Path(cfg.write_log_path)
    if not path.exists():
        return []

    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return lines[-limit:] if limit else lines
