"""M9 - classifying chat messages, and queueing the ones that could cause a write.

Chat does not go through the transcript pipeline, and the reason is worth
stating: a transcript is a continuous conversation where meaning spans turns,
so it is chunked with overlap and a context header. A channel is a list of
discrete messages, each classified on its own, with an id that must come back
attached to the right one.

So messages are batched per channel in timestamp order. Order matters even
though each is labelled separately, because "can you take a look?" needs the
message before it to be readable at all.

Three rules the model's output is checked against, in the wrapper's retry loop:
  every message_id must be one that was sent
  every message sent must come back, since a missing entry is not the same as
  one labelled noise and the difference changes the precision figure
  every quote must be a literal substring of that message's own text

Afterwards:
  noise is deleted, because the brief says discarded not stored
  decision, blocker and request become pending extractions, because each could
  produce a downstream write and must go through the same approval gate as M3
  question is stored and classified but not queued, because answering one
  writes nothing anywhere
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone

from app.config import Settings, get_settings
from app.db import database
from app.db.repositories import chat as chat_repo
from app.db.repositories import extractions as extraction_repo
from app.db.repositories import sources as source_repo
from app.errors import ConsentRefused, LLMError, NotFoundError
from app.extraction.llm.client import call_structured
from app.extraction.llm.factory import get_llm_provider
from app.extraction.prompts import load_prompt
from app.ingestion.normaliser import normalise_text
from app.models.common import ExtractionType, SignalClass, StrictModel
from app.models.extraction import DraftSignalList, Extraction

logger = logging.getLogger("agent.signals")

PROMPT_NAME = "classify_signals"

#: Labels that could become something written somewhere, and therefore need a
#: human to approve them. A question produces no downstream write.
QUEUED_CLASSES = (SignalClass.DECISION, SignalClass.BLOCKER, SignalClass.REQUEST)

#: Messages per model call. Small enough that one bad batch costs little, large
#: enough that seventy-eight messages do not cost seventy-eight requests
#: against a daily allowance of twenty.
BATCH_SIZE = 20


class SignalRun(StrictModel):
    source_id: str
    prompt_version: str
    provider: str
    model: str

    batches: int = 0
    messages_seen: int = 0
    classified: int = 0
    noise_discarded: int = 0
    queued: int = 0
    by_class: dict[str, int] = {}

    failed_batches: list[str] = []
    duration_ms: int = 0

    @property
    def ok(self) -> bool:
        return not self.failed_batches


def render_batch(messages: list) -> str:
    """The model sees the export's own id, not the namespaced primary key.

    Short, and it is the id a person reading the channel would recognise. The
    mapping back to the stored row happens here, where the batch is in hand.
    """
    return "\n".join(
        f"[{m.external_id}] {m.author} at {m.ts} in #{m.channel}\n    {m.text}"
        for m in messages
    )


def _validator(batch: list):
    """Checked inside the retry loop, so a bad batch is repaired not lost."""
    wanted = {m.external_id: normalise_text(m.text) for m in batch}

    def validate(value: DraftSignalList) -> str | None:
        returned = {s.message_id for s in value.signals}

        unknown = returned - set(wanted)
        if unknown:
            return (
                f"these message ids were not in the batch: {sorted(unknown)[:3]}. "
                f"Copy the id exactly as it appears in square brackets."
            )

        missing = set(wanted) - returned
        if missing:
            return (
                f"{len(missing)} message(s) came back with no label, starting with "
                f"{sorted(missing)[:3]}. Return an entry for every message, including "
                f"the ones you label noise."
            )

        for signal in value.signals:
            if normalise_text(signal.quote) not in wanted[signal.message_id]:
                return (
                    f"the quote {signal.quote!r} does not appear in message "
                    f"{signal.message_id}. Copy the words exactly from that message."
                )
        return None

    return validate


def classify_signals(
    source_id: str,
    settings: Settings | None = None,
    *,
    replace_pending: bool = True,
) -> SignalRun:
    """Run M9 over one ingested chat export."""
    cfg = settings or get_settings()
    started = time.perf_counter()

    with database.connect(cfg) as conn:
        source = source_repo.get_source(conn, source_id)
        if source is None:
            raise NotFoundError(f"no source with id {source_id}")
        messages = chat_repo.list_messages(conn, source_id=source_id)

    # The same gate as every other capability, in the last place before a
    # transcript or a channel would leave the process.
    if not source.consent_flag:
        raise ConsentRefused(f"source {source_id} withheld consent. Nothing is sent to a model.")
    if not messages:
        raise NotFoundError(f"source {source_id} has no stored chat messages")

    prompt = load_prompt(PROMPT_NAME)
    provider = get_llm_provider(cfg)

    run = SignalRun(
        source_id=source_id,
        prompt_version=prompt.version_tag,
        provider=provider.name,
        model=provider.model,
        messages_seen=len(messages),
    )

    # Batched per channel, in timestamp order. Order matters even though each
    # message is labelled on its own: "can you take a look?" is unreadable
    # without the message before it.
    by_channel: dict[str, list] = {}
    for message in messages:
        by_channel.setdefault(message.channel, []).append(message)

    labelled: list[tuple[object, object]] = []

    for channel, channel_messages in sorted(by_channel.items()):
        for start in range(0, len(channel_messages), BATCH_SIZE):
            batch = channel_messages[start : start + BATCH_SIZE]
            label = f"{channel}[{start}:{start + len(batch)}]"
            run.batches += 1

            context = (
                f"CONTEXT (background only, never quote from this block)\n"
                f"  Channel : #{channel}\n"
                f"  Export  : {source.title}\n"
                f"  Batch   : {len(batch)} messages, in the order they were sent"
            )

            try:
                result = call_structured(
                    PROMPT_NAME,
                    prompt.render(context=context, messages=render_batch(batch)),
                    DraftSignalList,
                    source_id=source_id,
                    prompt_version=prompt.version_tag,
                    validators=[_validator(batch)],
                    settings=cfg,
                )
            except LLMError as exc:
                logger.warning("batch %s failed: %s", label, exc)
                run.failed_batches.append(label)
                continue

            by_id = {m.external_id: m for m in batch}
            for signal in result.signals:
                labelled.append((signal, by_id[signal.message_id]))

    # --- store ---------------------------------------------------------------
    # Stored ids, not the export's. discard() deletes by primary key.
    noise_ids = [m.id for s, m in labelled if s.classification is SignalClass.NOISE]
    now = datetime.now(timezone.utc).isoformat()

    with database.transaction(cfg) as conn:
        if replace_pending:
            extraction_repo.delete_for_source(conn, source_id, ExtractionType.SIGNAL)

        for signal, message in labelled:
            run.by_class[signal.classification.value] = run.by_class.get(signal.classification.value, 0) + 1

            if signal.classification is SignalClass.NOISE:
                continue

            chat_repo.classify(conn, message.id, signal.classification, signal.confidence)
            run.classified += 1

            if signal.classification not in QUEUED_CLASSES:
                continue

            payload = {
                "classification": signal.classification.value,
                "text": message.text,
                "reason": signal.reason,
                "channel": message.channel,
                "author": message.author,
                "message_id": message.external_id,
            }
            extraction_repo.insert(
                conn,
                Extraction(
                    id=f"{source_id}::signal::{uuid.uuid4().hex[:8]}",
                    source_id=source_id,
                    extraction_type=ExtractionType.SIGNAL,
                    payload=payload,
                    original_payload=payload,
                    verbatim_quote=signal.quote,
                    quote_verified=normalise_text(signal.quote) in normalise_text(message.text),
                    message_id=message.id,
                    speaker=message.author,
                    timestamp=message.ts,
                    confidence=signal.confidence,
                    dedup_key=message.id,
                    provider=provider.name,
                    model_name=provider.model,
                    prompt_version=prompt.version_tag,
                    created_at=now,
                ),
                expiry_hours=cfg.pending_expiry_hours,
            )
            run.queued += 1

        run.noise_discarded = chat_repo.discard(conn, noise_ids)

    run.duration_ms = int((time.perf_counter() - started) * 1000)
    logger.info(
        "classified %s of %s messages from %s, discarded %s as noise, queued %s",
        run.classified, run.messages_seen, source_id, run.noise_discarded, run.queued,
    )
    return run
