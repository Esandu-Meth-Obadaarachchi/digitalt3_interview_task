"""HTTP surface for M14.

One endpoint runs the loop and returns the whole trace. The trace is not
debugging output: an answer produced by a sequence of tool calls is only worth
as much as a reader's ability to see which calls produced it.

There is no endpoint for approving, writing or sending here, and there is none
in the toolbelt either. The loop reaches the same review queue everything else
reaches, and a person still decides.
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Query

from app.agent.graph import run_agent
from app.agent.tools import TOOLS
from app.config import get_settings
from app.models.agent import AgentRun
from app.models.common import StrictModel

router = APIRouter(prefix="/api/agent", tags=["agent"])


class RunRequest(StrictModel):
    instruction: str
    #: Optional override, capped so a request cannot ask for an unbounded loop.
    max_steps: int | None = None
    #: Metadata filter. The tools enforce it, so a run scoped to one project
    #: cannot read another even if the model asks.
    sources: list[str] | None = None


@router.get("/tools", summary="What the loop is allowed to do")
def list_tools() -> list[dict]:
    """Names and descriptions, plus whether a tool writes anything.

    Returned so the interface can show the boundary rather than assert it.
    """
    return [
        {
            "name": tool.name,
            "description": (tool.description or "").strip().splitlines()[0],
            "writes": tool.name == "propose_action_item",
        }
        for tool in TOOLS
    ]


@router.post("", response_model=AgentRun, summary="Run the loop on one instruction")
def run(request: RunRequest = Body(...), max_steps: int | None = Query(default=None)) -> AgentRun:
    settings = get_settings()
    budget = request.max_steps or max_steps or settings.agent_max_steps
    return run_agent(
        request.instruction,
        settings,
        max_steps=min(budget, 20),
        sources=set(request.sources) if request.sources else None,
    )
