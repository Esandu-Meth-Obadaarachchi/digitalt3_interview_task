"""A mock tracker backed by a local table (M7).

Stands in for a foreign work tracker. `tracker_items` is its store, seeded with
a messy pre-existing backlog the agent never created, because the adapter
contract requires that mocks return realistically messy data.

This class implements TrackerAdapter and nothing above the interface knows it
exists. `app/adapters/factory.py` is the only module that names it. Swapping it
for a real client means writing one class and changing TRACKER_PROVIDER.

The inspectable write log deliberately does NOT live here. Recording what the
agent attempted is the agent's audit, not tracker behaviour, so it sits in
`app/tracker/write_log.py` and works identically whichever adapter is in use.
An earlier version put it on the mock, which made the tracker service import a
concrete implementation. A test caught it.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.config import Settings, get_settings
from app.adapters.tracker import TrackerAdapter
from app.db import database
from app.db.repositories import tracker as tracker_repo
from app.errors import NotFoundError
from app.models.tracker import TrackerFilter, TrackerItem, TrackerItemDraft

logger = logging.getLogger("agent.tracker")


class MockTracker(TrackerAdapter):
    provider = "mock"

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()

    # --- the interface ------------------------------------------------------

    def create_item(self, draft: TrackerItemDraft) -> TrackerItem:
        now = _now()
        with database.transaction(self._settings) as conn:
            external_ref = tracker_repo.next_reference(conn)
            item = TrackerItem(
                external_ref=external_ref,
                title=draft.title,
                description=draft.description,
                assignee=draft.assignee,
                status="open",
                due_date=draft.due_date,
                labels=draft.labels,
                source_ref=draft.source_ref,
                created_at=now,
                updated_at=now,
            )
            tracker_repo.insert_item(conn, item, seeded=False)

        logger.info("mock tracker created %s from %s", external_ref, draft.source_ref)
        return item

    def get_item(self, external_ref: str) -> TrackerItem | None:
        with database.connect(self._settings) as conn:
            return tracker_repo.get_item(conn, external_ref)

    def list_items(self, criteria: TrackerFilter | None = None) -> list[TrackerItem]:
        with database.connect(self._settings) as conn:
            return tracker_repo.list_items(conn, criteria or TrackerFilter())

    def transition(self, external_ref: str, status: str) -> TrackerItem:
        with database.transaction(self._settings) as conn:
            if tracker_repo.get_item(conn, external_ref) is None:
                raise NotFoundError(f"the tracker has no item {external_ref}")
            tracker_repo.update_status(conn, external_ref, status, _now())
            return tracker_repo.get_item(conn, external_ref)

    # --- seeding ------------------------------------------------------------

    def seed(self, items: list[dict]) -> int:
        """Load the pre-existing backlog.

        These items carry no source_ref: the agent did not create them, and
        must work correctly alongside them.
        """
        with database.transaction(self._settings) as conn:
            tracker_repo.clear_seeded(conn)
            for raw in items:
                tracker_repo.insert_item(
                    conn,
                    TrackerItem(
                        external_ref=raw["external_ref"],
                        title=raw["title"],
                        description=raw.get("description") or None,
                        assignee=raw.get("assignee"),
                        status=raw["status"],
                        due_date=raw.get("due_date"),
                        labels=raw.get("labels", []),
                        source_ref=None,
                        created_at=raw.get("created_at", _now()),
                        updated_at=raw.get("updated_at", _now()),
                    ),
                    seeded=True,
                )
        return len(items)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
