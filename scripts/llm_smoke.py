#!/usr/bin/env python
"""Prove the model path end to end against one real chunk.

Not a test: the test suite runs against a deterministic stub so it never needs
a network or a key. This is the check that the configured provider actually
answers, that its structured output validates, and that the repair loop and the
cache behave against a live model.

    make llm-smoke                      # configured provider
    make llm-smoke PROVIDER=ollama      # against a local model instead

Prints the extracted actions, whether each quote is a literal substring of the
transcript, and what the call cost in attempts, tokens and milliseconds.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.config import get_settings  # noqa: E402
from app.db import database  # noqa: E402
from app.db.repositories import llm_calls as llm_call_repo  # noqa: E402
from app.db.repositories import segments as segment_repo  # noqa: E402
from app.db.repositories import sources as source_repo  # noqa: E402
from app.errors import AgentError  # noqa: E402
from app.extraction.chunker import chunk_segments  # noqa: E402
from app.extraction.llm.client import call_structured  # noqa: E402
from app.extraction.llm.factory import get_llm_provider  # noqa: E402
from app.extraction.prompts import load_prompt  # noqa: E402
from app.ingestion.normaliser import normalise_text  # noqa: E402
from app.models.common import UNSPECIFIED, Confidence, StrictModel  # noqa: E402

GREEN, RED, YELLOW, DIM, BOLD, OFF = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[1m", "\033[0m"


class DraftAction(StrictModel):
    """The shape the model must return. Phase 3 promotes this to the real
    contract with owner and date discipline enforced by the eval harness."""

    what: str
    owner: str
    due_date: str
    verbatim_quote: str
    speaker: str
    timestamp: str
    confidence: Confidence


class DraftActions(StrictModel):
    actions: list[DraftAction]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default="meeting-sprint-planning-2024-11-18")
    parser.add_argument("--chunk", type=int, default=1, help="which chunk to send (0-based)")
    parser.add_argument("--no-cache", action="store_true")
    parser.add_argument(
        "--provider",
        choices=("gemini", "ollama", "fake"),
        help="override LLM_PROVIDER for this run, to compare two models on the same chunk",
    )
    args = parser.parse_args()

    overrides: dict[str, object] = {}
    if args.no_cache:
        overrides["llm_cache_enabled"] = False
    if args.provider:
        overrides["llm_provider"] = args.provider
    settings = get_settings().model_copy(update=overrides)

    provider = get_llm_provider(settings)
    usable, reason = provider.available()
    print(f"\n{BOLD}provider{OFF} {provider.describe()}  {GREEN if usable else RED}{reason}{OFF}")
    if not usable:
        print(f"\n{RED}cannot run: {reason}{OFF}\n")
        return 1

    with database.connect(settings) as conn:
        source = source_repo.get_source(conn, args.source)
        if source is None:
            print(f"{RED}no source {args.source}. Run `make seed` first.{OFF}")
            return 1
        segments = segment_repo.list_segments(conn, args.source)
        source_text = segment_repo.get_source_text(conn, args.source)

    chunks = chunk_segments(source, segments, settings)
    if not chunks:
        print(f"{RED}no chunks for {args.source}{OFF}")
        return 1
    chunk = chunks[min(args.chunk, len(chunks) - 1)]

    prompt = load_prompt("extract_actions")
    print(f"{BOLD}prompt  {OFF} extract_actions v{prompt.version_tag}")
    print(f"{BOLD}chunk   {OFF} {chunk.label}, segments {chunk.first_segment_index}"
          f"-{chunk.last_segment_index}, ~{chunk.estimated_tokens} tokens\n")

    def quotes_must_be_verbatim(value: DraftActions) -> str | None:
        for action in value.actions:
            if normalise_text(action.verbatim_quote) not in source_text:
                return (
                    f"the quote {action.verbatim_quote!r} is not a literal substring of the "
                    f"transcript. Copy the words exactly as they appear."
                )
        return None

    try:
        result = call_structured(
            "extract_actions",
            prompt.render(context=chunk.context, chunk=chunk.text),
            DraftActions,
            source_id=source.id,
            prompt_version=prompt.version_tag,
            validators=[quotes_must_be_verbatim],
            settings=settings,
        )
    except AgentError as exc:
        print(f"{RED}{type(exc).__name__}: {exc}{OFF}\n")
        return 1

    print(f"{BOLD}{len(result.actions)} action(s){OFF}\n")
    for action in result.actions:
        verified = normalise_text(action.verbatim_quote) in source_text
        mark = f"{GREEN}verbatim{OFF}" if verified else f"{RED}FABRICATED{OFF}"
        owner = f"{YELLOW}{action.owner}{OFF}" if action.owner == UNSPECIFIED else action.owner
        due = f"{YELLOW}{action.due_date}{OFF}" if action.due_date == UNSPECIFIED else action.due_date
        print(f"  {action.what}")
        print(f"    {DIM}owner{OFF} {owner}   {DIM}due{OFF} {due}   "
              f"{DIM}conf{OFF} {action.confidence:.2f}   {mark}")
        print(f"    {DIM}[{action.timestamp}] {action.speaker}: \"{action.verbatim_quote[:90]}\"{OFF}\n")

    with database.connect(settings) as conn:
        usage = llm_call_repo.summarise(conn, capability="extract_actions")

    print(f"{BOLD}cost{OFF}  {usage.attempts} attempt(s) for {usage.calls} call(s), "
          f"retry rate {usage.retry_rate:.0%}, cache hits {usage.cache_hits}")
    print(f"      {usage.prompt_tokens} prompt + {usage.completion_tokens} completion tokens, "
          f"{usage.total_latency_ms} ms")
    print(f"      outcomes: {usage.outcomes}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
