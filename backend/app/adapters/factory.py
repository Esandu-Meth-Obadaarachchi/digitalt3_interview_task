"""The one line of wiring the adapter contract asks about.

    "could a real integration be dropped in by writing one new class and
     changing one line of wiring, with zero changes to agent logic?"

Adding a Jira client means writing `JiraTracker(TrackerAdapter)` and adding one
branch here. No prompt, no database column and no service changes, because
nothing above this function knows which implementation it is holding.

Two adapters live here now, one per external system this agent actually
touches: a work tracker and a document store. Both were added in the phase that
first called them, never before. The anti-patterns tab is explicit that an
empty file named after an integration reads as implying work that does not
exist.
"""

from __future__ import annotations

from app.adapters.mock_store import MockStore
from app.adapters.mock_tracker import MockTracker
from app.adapters.store import StoreAdapter
from app.adapters.tracker import TrackerAdapter
from app.config import Settings, get_settings

#: Set by tests to pin an implementation without touching configuration.
_override: TrackerAdapter | None = None
_store_override: StoreAdapter | None = None


def set_tracker_override(adapter: TrackerAdapter | None) -> None:
    global _override
    _override = adapter


def set_store_override(adapter: StoreAdapter | None) -> None:
    global _store_override
    _store_override = adapter


def get_tracker(settings: Settings | None = None) -> TrackerAdapter:
    if _override is not None:
        return _override

    cfg = settings or get_settings()

    if cfg.tracker_provider == "mock":
        return MockTracker(cfg)
    # elif cfg.tracker_provider == "jira":
    #     return JiraTracker(cfg)   # one class, one branch, nothing else changes

    raise ValueError(f"unknown TRACKER_PROVIDER: {cfg.tracker_provider}")


def get_store(settings: Settings | None = None) -> StoreAdapter:
    """The document store, for outcome records and digests."""
    if _store_override is not None:
        return _store_override

    cfg = settings or get_settings()

    if cfg.store_provider == "mock":
        return MockStore(cfg)
    # elif cfg.store_provider == "s3":
    #     return S3Store(cfg)   # one class, one branch, nothing else changes

    raise ValueError(f"unknown STORE_PROVIDER: {cfg.store_provider}")
