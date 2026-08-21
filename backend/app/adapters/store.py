"""The document store interface (M10, M11).

The second external system this agent touches. It needs somewhere to put an
outcome record a downstream agent will read, and somewhere to put a digest a
person will read. Neither belongs in the database: both are artefacts meant to
leave this system, and a delivery agent should not need a SQLite file to
consume one.

Four operations, each with a caller. `write` and `read` for the artefacts,
`list_documents` so the interface can show what exists, `exists` because
overwriting a versioned record silently would be the wrong behaviour and the
check has to be somebody's job.

Nothing here mentions a filesystem. A real implementation against object
storage or a document platform satisfies the same four methods.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.models.common import StrictModel


class StoredDocument(StrictModel):
    """A document as the store holds it."""

    key: str
    content_type: str = "application/json"
    size_bytes: int = 0
    written_at: str | None = None
    location: str | None = None


class StoreAdapter(ABC):
    """Somewhere to put an artefact that is meant to leave this system."""

    provider: str

    @abstractmethod
    def write(self, key: str, content: str, content_type: str = "application/json") -> StoredDocument:
        """Write a document and return it as the store now holds it."""

    @abstractmethod
    def read(self, key: str) -> str | None:
        """Return the document's content, or None when there is no such key."""

    @abstractmethod
    def list_documents(self, prefix: str | None = None) -> list[StoredDocument]:
        """List what exists, optionally under a prefix."""

    @abstractmethod
    def exists(self, key: str) -> bool:
        """Whether a key is already taken.

        Separate from `read` because the caller often needs to know without
        paying to fetch the content, and because silently overwriting a
        versioned record is the wrong behaviour and someone has to check.
        """

    def describe(self) -> str:
        return self.provider
