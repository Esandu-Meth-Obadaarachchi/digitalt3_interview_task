"""FastAPI application entry point.

Routers do routing, validation and serialisation. Every business rule lives in
a service or a repository, so the same rule holds whether it was reached
through HTTP, through the CLI, or from the eval harness. That matters here
specifically: the approval gate must not be something the API applies.

Domain errors are translated to status codes in one place, so a consent
refusal is a 403 with a stated reason rather than a 500 with a stack trace.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.db import database
from app.errors import AgentError
from app.routers import (
    agent,
    chat,
    digests,
    extractions,
    followups,
    outcome,
    qa,
    review,
    sources,
    tracker,
)

logger = logging.getLogger("agent")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)-7s %(name)s %(message)s")
    settings.ensure_directories()
    database.init_db(settings)
    # A real scheduler, started with the application. The capability test is
    # explicit that a button with nothing behind it is a partial implementation,
    # so the jobs are registered here and their next fire times are readable at
    # /api/digests/schedule.
    from app.scheduler import jobs

    jobs.start(settings)

    logger.info(
        "ready: db=%s provider=%s retrieval=%s tracker=%s scheduler=%s",
        settings.db_path,
        settings.llm_provider,
        settings.retrieval_mode,
        settings.tracker_provider,
        "on" if settings.scheduler_enabled else "off",
    )
    try:
        yield
    finally:
        jobs.shutdown()


app = FastAPI(
    title="Meeting & Channel Intelligence Agent",
    version="0.2.0",
    summary="Traceable, human-approved records from meeting transcripts and chat exports.",
    description=(
        "Every extracted item is anchored to a verbatim quote from its source and "
        "must be approved by a human before it reaches any external system."
    ),
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(AgentError)
async def handle_agent_error(request: Request, exc: AgentError) -> JSONResponse:
    """One place where domain errors become HTTP responses."""
    logger.warning("%s on %s: %s", exc.code, request.url.path, exc)
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.code, "detail": str(exc)},
    )


@app.get("/health", tags=["meta"], summary="Liveness and configuration")
def health() -> dict[str, object]:
    """What the system is wired to right now.

    `llm_key_present` reports whether a key is configured without ever
    returning the key itself.
    """
    from app.extraction.llm.factory import get_llm_provider

    settings = get_settings()
    provider = get_llm_provider(settings)
    usable, reason = provider.available()

    return {
        "status": "ok",
        "schema_version": database.schema_version(settings),
        "llm_provider": settings.llm_provider,
        "llm_model": provider.model,
        "llm_key_present": bool(settings.gemini_api_key),
        "llm_available": usable,
        "llm_detail": reason,
        "retrieval_mode": settings.retrieval_mode,
        "tracker_provider": settings.tracker_provider,
    }


app.include_router(sources.router)
app.include_router(extractions.router)
app.include_router(review.router)
app.include_router(tracker.router)
app.include_router(qa.router)
app.include_router(chat.router)
app.include_router(outcome.router)
app.include_router(digests.router)
app.include_router(followups.router)
app.include_router(agent.router)
