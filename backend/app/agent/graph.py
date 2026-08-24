"""M14 - the tool-dispatch loop, as a LangGraph state machine.

    plan ──► act ──► plan ──► … ──► answer
      │                        │
      └── no tool call ────────┘        budget spent ──► answer with what it has

Two nodes and one conditional edge. The model plans, the graph executes
whichever tools it asked for, the observations go back, and it plans again. It
ends when the model replies without a tool call, or when the step budget runs
out.

**Why a graph rather than a while loop.** The loop itself is four lines either
way. What the graph gives is the state as a value: every message, every tool
result and the step count live in one object the run returns, so the trace shown
to a reviewer is the thing the loop actually executed rather than a log written
alongside it. The budget is enforced on an edge, where it is visible, instead of
inside a condition somebody can forget.

**What the loop cannot do.** Nothing in `tools.py` approves an extraction,
writes to a tracker or sends a message. The agent may read anything and propose
into the review queue, and a person still holds all three gates. That boundary
is the reason this is safe to let plan freely.
"""

from __future__ import annotations

import json
import logging
import operator
import time
import uuid
from datetime import datetime, timezone
from typing import Annotated, TypedDict

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages

from app.agent.model import get_chat_model
from app.agent.tools import TOOLS, TOOLS_BY_NAME, scope_label, set_scope
from app.config import Settings, get_settings
from app.models.agent import AgentRun, ToolCall

logger = logging.getLogger("agent.loop")

SYSTEM_PROMPT = """You are the analyst for a meeting intelligence system.

You answer by looking things up with the tools, never from memory. You have not
read these meetings before this conversation.

RULES

1. Quote the evidence. Any claim about what somebody said or agreed must carry
   the words from a tool observation. Do not paraphrase a quote.

2. Say when you do not know. If the tools do not contain the answer, say so and
   say what you looked for. An answer nobody can check is worse than no answer.

3. Never invent an owner or a date. If nobody was named, say nobody was named.

4. You cannot approve anything, write to a tracker or send anything. Those need
   a person. If the instruction asks for one, do the part you can and say which
   part needs a human.

5. Work in steps. Call one or two tools, read what came back, then decide what
   to do next. Every observation tells you which step you are on and how many
   remain. Answer as soon as you have enough: a run ending on the budget is a
   run that browsed instead of deciding. Repeating a search with a different
   single word is browsing.

Finish with a short answer in plain prose, with the quotes you relied on."""


class AgentState(TypedDict):
    messages: Annotated[list, add_messages]
    steps: int
    budget: int
    #: Accumulated rather than replaced. Without the reducer the state keeps
    #: only the last node's trace and the run reports its final step as its
    #: whole history.
    trace: Annotated[list, operator.add]


def _plan_node(model):
    def plan(state: AgentState) -> dict:
        if state["steps"] >= state["budget"]:
            # The budget is spent. One last call, with tools withheld, so the
            # model has to answer with what it already has rather than asking
            # for one more thing it will not get.
            final = model.invoke(
                state["messages"]
                + [
                    HumanMessage(
                        content=(
                            "You have used your tool budget. Answer now with what you already "
                            "have, and say plainly what you could not check."
                        )
                    )
                ]
            )
            return {"messages": [AIMessage(content=_text_of(final.content))]}
        return {"messages": [model.invoke(state["messages"])]}

    return plan


def _act(state: AgentState) -> dict:
    """Run whichever tools the model asked for, and record each one."""
    last = state["messages"][-1]
    outputs, trace = [], []

    for call in getattr(last, "tool_calls", []) or []:
        started = time.perf_counter()
        name, arguments = call["name"], call.get("args", {})
        tool = TOOLS_BY_NAME.get(name)

        if tool is None:
            # A model asking for a tool nobody wrote is told so, rather than
            # having the run fail. It is a recoverable mistake.
            observation, ok, error = (
                f"there is no tool called {name}. Available: {', '.join(TOOLS_BY_NAME)}",
                False,
                "unknown tool",
            )
        else:
            try:
                observation, ok, error = str(tool.invoke(arguments)), True, None
            except Exception as exc:  # noqa: BLE001
                observation, ok, error = f"{name} failed: {exc}", False, str(exc)[:300]
                logger.warning("tool %s failed: %s", name, exc)

        # The step counter rides on the observation. The model is otherwise
        # blind to its own budget and spends it searching, which is exactly what
        # the first real run did: four single-word searches and no answer.
        step_number = state["steps"] + len(trace) + 1
        remaining = max(0, state["budget"] - step_number)
        outputs.append(
            ToolMessage(
                content=f"[step {step_number} of {state['budget']}, {remaining} left]\n{observation}",
                tool_call_id=call["id"],
                name=name,
            )
        )
        trace.append(
            ToolCall(
                step=state["steps"] + len(trace) + 1,
                tool=name,
                arguments=arguments,
                observation=observation[:1200],
                observation_chars=len(observation),
                ok=ok,
                error=error,
                duration_ms=int((time.perf_counter() - started) * 1000),
            )
        )

    return {"messages": outputs, "steps": state["steps"] + len(trace), "trace": trace}


def _should_continue(state: AgentState) -> str:
    last = state["messages"][-1]
    return "act" if getattr(last, "tool_calls", None) else END


def build_graph(model):
    """The whole machine. Two nodes, one loop, one exit."""
    graph = StateGraph(AgentState)
    graph.add_node("plan", _plan_node(model))
    graph.add_node("act", _act)
    graph.set_entry_point("plan")
    graph.add_conditional_edges("plan", _should_continue, {"act": "act", END: END})
    graph.add_edge("act", "plan")
    return graph.compile()


def run_agent(
    instruction: str,
    settings: Settings | None = None,
    *,
    max_steps: int | None = None,
    sources: set[str] | None = None,
) -> AgentRun:
    """One instruction, run to an answer or to the end of the budget.

    `sources` is a hard ceiling on what the tools may read. It is enforced
    inside the tools rather than asked for in the prompt, so a model that
    forgets the instruction still cannot reach another project.
    """
    cfg = settings or get_settings()
    set_scope(sources)
    run_id = str(uuid.uuid4())
    budget = max_steps or cfg.agent_max_steps
    started = time.perf_counter()

    model, _ = get_chat_model(cfg, run_id=run_id)
    machine = build_graph(model.bind_tools(TOOLS))

    run = AgentRun(
        id=run_id,
        instruction=instruction,
        step_budget=budget,
        tools_available=list(TOOLS_BY_NAME),
        scope=sorted(sources) if sources else [],
        provider=cfg.agent_provider,
        model=cfg.gemini_model if cfg.agent_provider == "gemini" else "scripted",
        started_at=datetime.now(timezone.utc).isoformat(),
    )

    try:
        final = machine.invoke(
            {
                "messages": [
                    SystemMessage(content=SYSTEM_PROMPT),
                    SystemMessage(
                        content=(
                            f"SCOPE: this run may read {scope_label()}. The tools enforce it, "
                            f"so a call outside it is refused rather than answered. Use "
                            f"focus_on_source to narrow further once you know which source "
                            f"matters."
                        )
                    ),
                    HumanMessage(content=instruction),
                ],
                "steps": 0,
                "budget": budget,
                "trace": [],
            },
            # One more than the budget allows for the closing answer, and the
            # graph counts a plan and an act separately.
            {"recursion_limit": budget * 2 + 4},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("agent run failed: %s", exc)
        run.stop_reason = "model_error"
        run.answer = f"the run could not be completed: {exc}"
        run.duration_ms = int((time.perf_counter() - started) * 1000)
        return run

    run.steps = [step for step in final.get("trace", [])]
    run.steps_used = final.get("steps", 0)
    run.answer = _last_text(final["messages"])
    run.finished = True
    run.stop_reason = "step_budget" if run.steps_used >= budget else "answered"
    run.duration_ms = int((time.perf_counter() - started) * 1000)
    logger.info(
        "agent run %s: %s step(s), stop=%s", run_id[:8], run.steps_used, run.stop_reason
    )
    return run


def _text_of(content) -> str:
    """The readable text of one message.

    Gemini returns content as a list of parts rather than a string, so an
    answer rendered without this reaches the interface as a JSON blob starting
    [{"type": "text", …}]. Found by running the loop against the real model
    rather than only against the stub, which returns plain strings.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            part.get("text", "") if isinstance(part, dict) else str(part)
            for part in content
        ]
        return "\n".join(p for p in parts if p).strip()
    return str(content or "")


def _last_text(messages: list) -> str:
    for message in reversed(messages):
        if isinstance(message, AIMessage):
            text = _text_of(message.content)
            if text:
                return text
    return ""
