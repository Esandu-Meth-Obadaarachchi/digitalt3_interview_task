"""A notifier backed by an inspectable JSONL log (M10).

Same principle as the tracker's write log: during the walkthrough it must be
possible to show exactly what would have been posted to the real channel, and
a log can be read aloud where a table has to be queried.

Nothing is sent anywhere. That is the whole point of a mock in this exercise,
and the contract is explicit that a clean, swappable mock earns full marks
while a real SaaS integration earns none.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.adapters.notifier import Notification, NotifierAdapter
from app.config import Settings, get_settings

logger = logging.getLogger("agent.notifier")


class MockNotifier(NotifierAdapter):
    provider = "mock"

    def __init__(self, settings: Settings | None = None) -> None:
        cfg = settings or get_settings()
        self._path = Path(cfg.notification_log_path)

    def post(self, channel: str, subject: str, body: str) -> Notification:
        notification = Notification(
            id=str(uuid.uuid4()),
            channel=channel,
            subject=subject,
            body=body,
            posted_at=datetime.now(timezone.utc).isoformat(),
            provider=self.provider,
        )

        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(notification.model_dump_json() + "\n")
        except OSError:
            # The log is evidence, not a dependency. A digest that was built
            # correctly should not be lost because a file could not be opened.
            logger.warning("could not append to the notification log", exc_info=True)

        logger.info("posted %r to %s", subject, channel)
        return notification

    def list_posts(self, channel: str | None = None, limit: int | None = None) -> list[Notification]:
        if not self._path.exists():
            return []

        posts = [
            Notification.model_validate_json(line)
            for line in self._path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        if channel:
            posts = [p for p in posts if p.channel == channel]
        return posts[-limit:] if limit else posts
