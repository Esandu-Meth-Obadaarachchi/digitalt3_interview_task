"""A document store backed by the local filesystem (M10, M11).

Files rather than a table, deliberately. An outcome record exists so that a
downstream agent can consume it without touching the transcript store, and a
file on disk is the most honest stand-in for that: it can be opened, diffed,
copied and read by something that knows nothing about this application. During
the walkthrough it can be shown.

Keys are treated as relative paths and are checked to stay inside the
configured directory. A key containing "../" would otherwise write wherever it
liked, which is a real hazard the moment a key is derived from a source id that
came in over HTTP.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from app.adapters.store import StoreAdapter, StoredDocument
from app.config import Settings, get_settings
from app.errors import AgentError

logger = logging.getLogger("agent.store")


class MockStore(StoreAdapter):
    provider = "mock"

    def __init__(self, settings: Settings | None = None, root: Path | None = None) -> None:
        cfg = settings or get_settings()
        self._root = Path(root or cfg.document_store_dir)

    def _resolve(self, key: str) -> Path:
        """Refuse anything that would escape the store directory.

        Keys are derived from source ids, which arrive over HTTP. A key of
        "../../etc/thing" must not be writable, and the check belongs here
        rather than in every caller.
        """
        root = self._root.resolve()
        target = (root / key).resolve()
        if not target.is_relative_to(root):
            raise AgentError(f"key {key!r} would write outside the document store")
        return target

    def write(self, key: str, content: str, content_type: str = "application/json") -> StoredDocument:
        target = self._resolve(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

        logger.info("wrote %s to the document store", key)
        return StoredDocument(
            key=key,
            content_type=content_type,
            size_bytes=len(content.encode("utf-8")),
            written_at=datetime.now(timezone.utc).isoformat(),
            location=str(target),
        )

    def read(self, key: str) -> str | None:
        target = self._resolve(key)
        return target.read_text(encoding="utf-8") if target.exists() else None

    def exists(self, key: str) -> bool:
        return self._resolve(key).exists()

    def list_documents(self, prefix: str | None = None) -> list[StoredDocument]:
        root = self._root
        if not root.exists():
            return []

        documents = []
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            key = str(path.relative_to(root))
            if prefix and not key.startswith(prefix):
                continue
            stat = path.stat()
            documents.append(
                StoredDocument(
                    key=key,
                    content_type="application/json" if key.endswith(".json") else "text/markdown",
                    size_bytes=stat.st_size,
                    written_at=datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
                    location=str(path),
                )
            )
        return documents
