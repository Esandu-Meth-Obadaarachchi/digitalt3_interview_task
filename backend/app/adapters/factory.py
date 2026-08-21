"""The one line of wiring the adapter contract asks about.

    "could a real integration be dropped in by writing one new class and
     changing one line of wiring, with zero changes to agent logic?"

Adding a Jira client means writing `JiraTracker(TrackerAdapter)` and adding one
branch here. No prompt, no database column and no service changes, because
nothing above this function knows which implementation it is holding.

The document store and the notification adapters are not here. They are built
in Phase 8 where M10 and M11 call them. The anti-patterns tab is explicit that
an empty file named after an integration reads as implying work that does not
exist, so they are absent from the tree and marked Not built in the README.
"""

from __future__ import annotations

from app.adapters.mock_tracker import MockTracker
from app.adapters.tracker import TrackerAdapter
from app.config import Settings, get_settings

#: Set by tests to pin an implementation without touching configuration.
_override: TrackerAdapter | None = None


def set_tracker_override(adapter: TrackerAdapter | None) -> None:
    global _override
    _override = adapter


def get_tracker(settings: Settings | None = None) -> TrackerAdapter:
    if _override is not None:
        return _override

    cfg = settings or get_settings()

    if cfg.tracker_provider == "mock":
        return MockTracker(cfg)
    # elif cfg.tracker_provider == "jira":
    #     return JiraTracker(cfg)   # one class, one branch, nothing else changes

    raise ValueError(f"unknown TRACKER_PROVIDER: {cfg.tracker_provider}")
