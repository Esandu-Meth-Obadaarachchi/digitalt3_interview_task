"""The contracts for the tool-dispatch loop (M14).

An agent run is a sequence of decisions, and the sequence is the deliverable
rather than a side effect. A reviewer asked to trust an answer needs to see
which tools were called, in what order, with what arguments, and what came
back, so every step is a stored record rather than a log line.
"""

from __future__ import annotations

from pydantic import Field

from app.models.common import Citation, StrictModel


class ToolCall(StrictModel):
    """One tool the model chose, and what it got back."""

    step: int
    tool: str
    arguments: dict = Field(default_factory=dict)
    #: The observation, trimmed for display. The full text went to the model.
    observation: str
    observation_chars: int = 0
    ok: bool = True
    error: str | None = None
    duration_ms: int = 0


class AgentRun(StrictModel):
    """One instruction, the loop it produced, and what it concluded."""

    id: str
    instruction: str
    #: What the model said before each tool call, when it said anything.
    steps: list[ToolCall] = Field(default_factory=list)

    answer: str = ""
    #: Present only when the answer quotes stored evidence.
    citations: list[Citation] = Field(default_factory=list)
    #: True when the loop reached an answer rather than running out of budget.
    finished: bool = False
    #: Why it stopped: answered · step_budget · tool_error · model_error
    stop_reason: str = ""

    steps_used: int = 0
    step_budget: int = 0
    tools_available: list[str] = Field(default_factory=list)

    provider: str = ""
    model: str = ""
    prompt_version: str = ""
    duration_ms: int = 0
    started_at: str = ""

    @property
    def tool_names(self) -> list[str]:
        return [step.tool for step in self.steps]
