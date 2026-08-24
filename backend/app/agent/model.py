"""The chat model the loop plans with, and where its calls are recorded.

Two implementations behind one call, the same shape as the extraction
providers: Gemini for real use, and a scripted stub so the test suite drives the
whole graph with no key and no quota.

Every planning call lands in `llm_calls` with capability `agent`, through a
LangChain callback. Without it the agent would spend requests invisibly and the
usage panel would understate what a run cost, which is the sort of gap that
turns a cost claim into a guess.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage

from app.config import Settings, get_settings
from app.db import database
from app.db.repositories import llm_calls as llm_repo
from app.models.telemetry import CallOutcome, LLMCall

logger = logging.getLogger("agent.loop.model")


class CallRecorder(BaseCallbackHandler):
    """Writes one llm_calls row per planning call.

    Failures here are swallowed and logged. Telemetry must never be the reason
    a run fails, and a silent swallow with no log is how a retry rate reads as
    zero for two phases.
    """

    def __init__(self, settings: Settings, model: str, run_id: str) -> None:
        self._cfg = settings
        self._model = model
        self._run_id = run_id
        self.calls = 0

    def _write(self, outcome: CallOutcome, error: str | None = None, usage: dict | None = None) -> None:
        self.calls += 1
        try:
            with database.transaction(self._cfg) as conn:
                llm_repo.record_call(
                    conn,
                    LLMCall(
                        id=str(uuid.uuid4()),
                        call_id=self._run_id,
                        capability="agent",
                        provider="gemini",
                        model=self._model,
                        attempt=self.calls,
                        outcome=outcome,
                        prompt_tokens=(usage or {}).get("input_tokens"),
                        completion_tokens=(usage or {}).get("output_tokens"),
                        error=error,
                        created_at=datetime.now(timezone.utc).isoformat(),
                    ),
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not record an agent call: %s", exc)

    def on_llm_end(self, response, **kwargs: Any) -> None:
        usage = {}
        try:
            message = response.generations[0][0].message
            usage = getattr(message, "usage_metadata", None) or {}
        except Exception:  # noqa: BLE001
            usage = {}
        self._write(CallOutcome.OK, usage=usage)

    def on_llm_error(self, error: BaseException, **kwargs: Any) -> None:
        self._write(CallOutcome.ERROR, error=str(error)[:400])


class ScriptedChatModel(BaseChatModel):
    """A deterministic planner for the tests.

    Holds a list of AIMessages and returns them in order, so a test states the
    exact sequence of tool calls it wants the graph to execute. The last one
    should carry no tool calls, which is how the loop ends.
    """

    responses: list[AIMessage] = []
    index: int = 0

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs):  # noqa: ANN001, ARG002
        return self

    def _generate(self, messages: list[BaseMessage], stop=None, run_manager=None, **kwargs):  # noqa: ANN001, ARG002
        from langchain_core.outputs import ChatGeneration, ChatResult

        if self.index >= len(self.responses):
            reply = AIMessage(content="I have run out of scripted responses.")
        else:
            reply = self.responses[self.index]
            self.index += 1
        return ChatResult(generations=[ChatGeneration(message=reply)])


def get_chat_model(settings: Settings | None = None, *, run_id: str = "") -> tuple[Any, CallRecorder | None]:
    """The planner, and the recorder watching it.

    Returns the stub with no recorder when AGENT_PROVIDER is 'fake', because a
    stub run costs nothing and recording it would put rows in the telemetry
    table describing calls no provider ever received.
    """
    cfg = settings or get_settings()

    if cfg.agent_provider == "fake":
        return ScriptedChatModel(responses=list(_SCRIPT)), None

    from langchain_google_genai import ChatGoogleGenerativeAI

    recorder = CallRecorder(cfg, cfg.gemini_model, run_id or str(uuid.uuid4()))
    model = ChatGoogleGenerativeAI(
        model=cfg.gemini_model,
        google_api_key=cfg.gemini_api_key,
        temperature=0,
        callbacks=[recorder],
    )
    return model, recorder


#: Replaced by a test through `set_script`. Module level so a fixture can set it
#: without threading a model object through the graph.
_SCRIPT: list[AIMessage] = []


def set_script(responses: list[AIMessage]) -> None:
    """Point the stub at a sequence of planner replies. Tests only."""
    global _SCRIPT
    _SCRIPT = list(responses)
