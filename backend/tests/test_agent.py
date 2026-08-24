"""M14 - the tool-dispatch loop.

Three groups. What the loop can reach, which is the safety property. What the
loop does with a scripted planner, which is the mechanics. And what it does when
things go wrong, which is where a loop usually misbehaves.

The planner is scripted throughout. A test asserting a model chose the right
tool would be measuring the model, and that belongs in the eval harness.
"""

from __future__ import annotations

import pathlib

import pytest
from langchain_core.messages import AIMessage

from app.agent import tools as agent_tools
from app.agent.graph import run_agent
from app.agent.model import set_script
from app.db import database
from app.db.repositories import extractions as extraction_repo
from app.extraction.actions import extract_actions
from app.ingestion.service import ingest_from_manifest
from app.models.common import ExtractionType, ReviewStatus

SPRINT = "meeting-sprint-planning-2024-11-18"


def call(name: str, **arguments) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": arguments, "id": f"call_{name}"}],
    )


@pytest.fixture()
def agent_settings(settings, monkeypatch):
    monkeypatch.setenv("AGENT_PROVIDER", "fake")
    from app.config import get_settings

    get_settings.cache_clear()
    cfg = get_settings()
    ingest_from_manifest(cfg)
    return cfg


# --- what the loop can reach -------------------------------------------------


def test_no_tool_crosses_a_gate():
    """The safety property, asserted by name.

    A tool that approved, wrote to a tracker or sent a message would make the
    gates reachable by a model deciding it was confident.
    """
    names = set(agent_tools.TOOLS_BY_NAME)
    forbidden = {"approve", "write", "send", "reject", "sync", "emit", "post", "delete"}

    for name in names:
        assert not any(word in name for word in forbidden), f"{name} sounds like it crosses a gate"

    assert "propose_action_item" in names, "the loop should be able to propose"


def test_the_toolbelt_does_not_import_anything_that_writes_outward():
    """Structural, because the absence is the guarantee.

    A tool could be added tomorrow calling the tracker service directly. This
    fails the moment the module gains the import.
    """
    source = pathlib.Path(agent_tools.__file__).read_text(encoding="utf-8")

    assert "tracker.service" not in source
    assert "followup" not in source
    assert "from app.review import queue" in source, "reading the queue is allowed"
    assert "queue.approve" not in source
    assert "queue.reject" not in source


def test_every_tool_documents_itself():
    """The description is the interface. A model choosing between nine tools
    reads nothing else."""
    for tool in agent_tools.TOOLS:
        assert tool.description and len(tool.description) > 40, f"{tool.name} needs a real docstring"


# --- the mechanics -----------------------------------------------------------


def test_a_run_records_every_tool_call_in_order(agent_settings):
    set_script([
        call("list_sources"),
        call("read_transcript", source_id=SPRINT, count=3),
        AIMessage(content="Two meetings are stored and the sprint planning opens with Sarah Chen."),
    ])

    run = run_agent("what is stored", agent_settings)

    assert run.finished
    assert run.stop_reason == "answered"
    assert run.tool_names == ["list_sources", "read_transcript"]
    assert run.steps_used == 2
    assert [s.step for s in run.steps] == [1, 2]
    assert run.answer.startswith("Two meetings")


def test_each_step_carries_its_arguments_and_what_came_back(agent_settings):
    set_script([call("read_transcript", source_id=SPRINT, count=2), AIMessage(content="done")])

    step = run_agent("read it", agent_settings).steps[0]

    assert step.tool == "read_transcript"
    assert step.arguments == {"source_id": SPRINT, "count": 2}
    assert step.ok is True
    assert "segments in total" in step.observation
    assert step.observation_chars >= len(step.observation)


def test_the_step_budget_stops_the_loop(agent_settings):
    """A planner asking forever is the failure mode of every agent loop."""
    set_script([call("list_sources") for _ in range(20)])

    run = run_agent("go forever", agent_settings, max_steps=3)

    assert run.steps_used == 3
    assert run.stop_reason == "step_budget"
    assert run.finished, "a spent budget still produces an answer"


def test_an_unknown_tool_is_reported_back_rather_than_crashing(agent_settings):
    set_script([call("summon_a_pony", colour="pink"), AIMessage(content="no such tool, sorry")])

    run = run_agent("do something impossible", agent_settings)

    step = run.steps[0]
    assert step.ok is False
    assert "there is no tool called summon_a_pony" in step.observation
    assert run.finished


def test_a_failing_tool_does_not_end_the_run(agent_settings):
    set_script([
        call("list_extractions", extraction_type="nonsense"),
        AIMessage(content="that filter is not valid"),
    ])

    run = run_agent("list nonsense", agent_settings)

    assert run.steps[0].ok is False
    assert run.steps[0].error
    assert run.finished


# --- proposing, which is the only write ---------------------------------------


def test_a_proposal_lands_in_the_queue_as_pending(agent_settings, scripted_model):
    scripted_model()
    extract_actions(SPRINT, agent_settings)

    with database.connect(agent_settings) as conn:
        quote = extraction_repo.list_extractions(
            conn, source_id=SPRINT, extraction_type=ExtractionType.ACTION
        )[0].verbatim_quote

    set_script([
        call("propose_action_item", source_id=SPRINT, what="Something a human should check",
             verbatim_quote=quote),
        AIMessage(content="proposed one item"),
    ])

    run = run_agent("propose something", agent_settings)

    assert "status pending" in run.steps[0].observation
    with database.connect(agent_settings) as conn:
        proposed = [
            e for e in extraction_repo.list_extractions(conn, source_id=SPRINT)
            if e.payload.get("proposed_by") == "agent"
        ]
    assert len(proposed) == 1
    assert proposed[0].status is ReviewStatus.PENDING
    assert proposed[0].quote_verified is True


def test_a_proposal_with_a_quote_the_source_does_not_contain_is_refused(agent_settings):
    set_script([
        call("propose_action_item", source_id=SPRINT, what="Invented work",
             verbatim_quote="I will personally rewrite the entire kernel over the weekend"),
        AIMessage(content="it refused"),
    ])

    run = run_agent("propose something invented", agent_settings)

    assert "REFUSED" in run.steps[0].observation
    with database.connect(agent_settings) as conn:
        stored = extraction_repo.list_extractions(conn, source_id=SPRINT)
    assert not [e for e in stored if e.payload.get("proposed_by") == "agent"]


def test_a_proposal_for_an_unknown_source_is_refused(agent_settings):
    set_script([
        call("propose_action_item", source_id="no-such-meeting", what="x", verbatim_quote="y"),
        AIMessage(content="refused"),
    ])

    assert "REFUSED" in run_agent("propose", agent_settings).steps[0].observation
