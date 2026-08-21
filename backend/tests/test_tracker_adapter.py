"""M7 - the tracker adapter, its mock, and what it writes.

Two things are being checked. That the interface is honest, meaning every
operation on it has a caller and nothing above it knows which implementation it
is holding. And that the mock returns realistically messy data, because the
contract states plainly that "an agent that only works on clean data has not
been tested".
"""

from __future__ import annotations

import json

import pytest

from app.adapters.factory import get_tracker, set_tracker_override
from app.adapters.mock_tracker import MockTracker
from app.adapters.tracker import TrackerAdapter
from app.config import REPO_ROOT, Settings, get_settings
from app.db import database
from app.db.repositories import tracker as tracker_repo
from app.errors import NotFoundError
from app.models.common import UNSPECIFIED, ExtractionType
from app.models.extraction import Extraction
from app.models.tracker import TrackerFilter, TrackerItemDraft
from app.tracker.service import build_draft

SEED = json.loads((REPO_ROOT / "sample_data" / "tracker" / "seed_items.json").read_text())["items"]


@pytest.fixture()
def tracker(settings):
    adapter = get_tracker(settings)
    adapter.seed(SEED)
    return adapter


# --- the interface -----------------------------------------------------------


def test_the_factory_returns_something_implementing_the_interface(settings):
    set_tracker_override(None)
    assert isinstance(get_tracker(settings), TrackerAdapter)


def test_an_unknown_provider_is_rejected_at_configuration_time():
    """Better than failing in the factory: an unknown provider stops the
    process at startup rather than at the first write."""
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        Settings(tracker_provider="jira")  # type: ignore[arg-type]


def test_the_factory_still_refuses_a_provider_it_does_not_know():
    """The branch below the configuration check, reached only if the Literal
    is widened without the factory being updated with it."""
    set_tracker_override(None)
    unknown = Settings.model_construct(tracker_provider="jira")
    with pytest.raises(ValueError, match="unknown TRACKER_PROVIDER"):
        get_tracker(unknown)


def test_the_interface_names_only_operations_that_have_a_caller():
    """The contract asks for a narrow, honest interface. add_comment appears in
    the brief's illustrative example and is absent here because nothing in this
    build comments on a ticket."""
    assert set(TrackerAdapter.__abstractmethods__) == {
        "create_item", "get_item", "list_items", "transition"
    }


def test_nothing_above_the_interface_imports_the_mock():
    """The adapter contract's actual test: could a real integration be dropped
    in by writing one class and changing one line of wiring?"""
    import subprocess

    result = subprocess.run(
        ["grep", "-rln", "--include=*.py", "mock_tracker", str(REPO_ROOT / "backend" / "app")],
        capture_output=True, text=True,
    )
    importers = {line.rsplit("/", 1)[-1] for line in result.stdout.splitlines()}
    assert importers == {"factory.py"}, (
        "only the factory may name a concrete adapter. An earlier version had the "
        "tracker service importing MockTracker to append to the write log, which is "
        "the mock's shape leaking into agent logic. The log now lives in "
        "app/tracker/write_log.py and belongs to the agent, not the tracker."
    )


# --- the mock returns messy data ---------------------------------------------


def test_the_seeded_backlog_is_realistically_messy(tracker):
    items = tracker.list_items(TrackerFilter(written_by_agent=False))

    assert len(items) == 12
    assert sum(1 for i in items if i.assignee is None) >= 3
    assert sum(1 for i in items if i.due_date is None) >= 5
    assert sum(1 for i in items if not i.labels) >= 2
    assert len({i.status for i in items}) >= 8, "status is free text, not an enum"
    assert any(i.status != i.status.strip() for i in items), "trailing whitespace happens"
    assert any(i.due_date and i.due_date < "2024-06-01" for i in items), "stale due dates happen"


def test_the_seeded_backlog_contains_near_duplicates(tracker):
    titles = [i.title.lower() for i in tracker.list_items(TrackerFilter(written_by_agent=False))]
    assert any("login redirect" in t for t in titles)
    assert sum(1 for t in titles if "login redirect" in t) >= 2


def test_seeded_items_are_not_attributed_to_the_agent(tracker):
    for item in tracker.list_items(TrackerFilter(written_by_agent=False)):
        assert item.source_ref is None
        assert item.written_by_agent is False


def test_filtering_by_status_survives_inconsistent_formatting(tracker):
    """"In Progress " with trailing whitespace must still be findable."""
    assert tracker.list_items(TrackerFilter(status="in progress"))


def test_a_new_reference_continues_past_the_seeded_backlog(tracker):
    item = tracker.create_item(TrackerItemDraft(title="new", description="", source_ref="x"))
    assert item.external_ref == "MOCK-13"


def test_transitioning_an_unknown_item_is_a_clean_error(tracker):
    with pytest.raises(NotFoundError):
        tracker.transition("MOCK-999", "done")


def test_transition_accepts_free_text_status(tracker):
    """Real trackers let teams invent workflow states."""
    updated = tracker.transition("MOCK-1", "waiting on the client, chased twice")
    assert updated.status == "waiting on the client, chased twice"


def test_reseeding_replaces_the_backlog_but_keeps_agent_items(tracker, settings):
    tracker.create_item(TrackerItemDraft(title="agent wrote this", description="", source_ref="x"))
    tracker.seed(SEED)

    with database.connect(settings) as conn:
        assert tracker_repo.count_items(conn, written_by_agent=False) == 12
        assert tracker_repo.count_items(conn, written_by_agent=True) == 1


# --- what gets written -------------------------------------------------------


def _extraction(**payload) -> Extraction:
    body = {"what": "Finish the auth refactor", "owner": "Priya Sharma",
            "due_date": "2024-11-22", "due_date_stated": "Friday",
            "due_date_rule": "friday = the first friday after the meeting date"} | payload
    return Extraction(
        id="src::action::abc", source_id="src", extraction_type=ExtractionType.ACTION,
        payload=body, original_payload=body,
        verbatim_quote="I can have the refactor done with tests by Friday",
        quote_verified=True, speaker="Priya Sharma", timestamp="00:02:17",
        confidence=0.9, created_at="2024-11-18T10:00:00Z",
    )


def test_the_written_item_carries_the_source_id_timestamp_and_quote():
    """M7 requires it. A ticket saying only "finish the auth refactor" is a
    ticket nobody can check."""
    draft = build_draft(_extraction())

    assert "I can have the refactor done with tests by Friday" in draft.description
    assert "00:02:17" in draft.description
    assert "src" in draft.description
    assert "Priya Sharma" in draft.description
    assert draft.source_ref == "src::action::abc"


def test_an_unspecified_owner_becomes_no_assignee_not_a_guess():
    draft = build_draft(_extraction(owner=UNSPECIFIED))

    assert draft.assignee is None
    assert "needs-owner" in draft.labels
    assert "did not say who would do this" in draft.description


def test_an_unspecified_date_becomes_no_due_date():
    draft = build_draft(_extraction(due_date=UNSPECIFIED, due_date_stated=None))

    assert draft.due_date is None
    assert "needs-date" in draft.labels


def test_a_resolved_date_carries_the_rule_that_produced_it():
    draft = build_draft(_extraction())
    assert 'Stated timing: "Friday" -> 2024-11-22' in draft.description
    assert "first friday after the meeting date" in draft.description


def test_an_overridden_unverified_quote_is_flagged_on_the_ticket():
    """Someone reading the ticket three weeks later must be able to see that
    its quote was never verified."""
    extraction = _extraction()
    draft = build_draft(extraction.model_copy(update={"quote_verified": False}))

    assert "WARNING" in draft.description
    assert "could not be verified" in draft.description
    assert "unverified-quote" in draft.labels


def test_a_long_action_title_is_truncated_not_rejected():
    draft = build_draft(_extraction(what="x" * 400))
    assert len(draft.title) == 120


# --- the write log -----------------------------------------------------------


def test_the_write_log_is_append_only_jsonl(settings):
    from app.models.tracker import WriteOutcome
    from app.tracker import write_log

    write_log.append(settings, "a", WriteOutcome.CREATED, "mock", external_ref="MOCK-13")
    write_log.append(settings, "a", WriteOutcome.DEDUPLICATED, "mock", external_ref="MOCK-13")

    lines = settings.write_log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert [json.loads(line)["outcome"] for line in lines] == ["created", "deduplicated"]
    assert write_log.read(settings)[-1]["external_ref"] == "MOCK-13"


def test_a_write_log_failure_never_breaks_a_write(settings, monkeypatch):
    """The log is evidence, not a dependency."""
    from app.models.tracker import WriteOutcome
    from app.tracker import write_log

    monkeypatch.setattr(
        "pathlib.Path.open", lambda *a, **k: (_ for _ in ()).throw(OSError("read-only"))
    )
    write_log.append(settings, "a", WriteOutcome.CREATED, "mock")  # must not raise
