"""The notification interface (M10).

The third external system: somewhere a digest is posted for people to read.

Posting a digest is explicitly NOT gated by approval, and the task catalogue
says why: "posting the digest is not an external write. Digests never contain
unapproved extractions." The gate is upstream. By the time a digest exists,
every line in it came from something a human already approved, so there is
nothing left to approve. Putting a second gate here would ask a reviewer to
approve their own earlier approvals.

Two operations. `post` and `list_posts`, both with callers. There is no
`delete`, because nothing in this build retracts a notification and an
operation nobody uses is a promise nobody keeps.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.models.common import StrictModel


class Notification(StrictModel):
    """A posted message, as the channel now holds it."""

    id: str
    channel: str
    subject: str
    body: str
    posted_at: str
    provider: str


class NotifierAdapter(ABC):
    """Somewhere to post something for people to read."""

    provider: str

    @abstractmethod
    def post(self, channel: str, subject: str, body: str) -> Notification:
        """Post one message and return it as the channel now holds it."""

    @abstractmethod
    def list_posts(self, channel: str | None = None, limit: int | None = None) -> list[Notification]:
        """What has been posted, so the interface can show it."""

    def describe(self) -> str:
        return self.provider
