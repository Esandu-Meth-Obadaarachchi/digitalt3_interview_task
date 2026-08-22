"""M9 - chat ingestion and signal classification, and golden case 7.

    "Precision on the golden-labelled subset is at least 0.7 and the two
     direct-message records in the export are excluded from processing
     entirely."

The exclusion half is asserted at three depths, because it is the one property
here that cannot be walked back if it fails: a private conversation that
reaches the store has already reached it.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from app.config import REPO_ROOT
from app.db import database
from app.db.repositories import chat as chat_repo
from app.db.repositories import extractions as extraction_repo
from app.errors import ConsentRefused, NotFoundError
from app.extraction.signals import QUEUED_CLASSES, classify_signals
from app.ingestion.chat_export import is_direct_message, parse_chat_export, read_chat_export
from app.ingestion.service import ingest_chat_export, ingest_from_manifest
from app.models.common import ExtractionType, ReviewStatus, SignalClass, SourceStatus
from app.models.source import SourceMetadata

EXPORT = "chat-export-meridian-2024-11-20"
EXPORT_PATH = REPO_ROOT / "sample_data" / "chat_export" / "channels.json"
GOLDEN = json.loads((REPO_ROOT / "sample_data" / "golden" / "golden_signals.json").read_text())


@pytest.fixture()
def ingested(settings):
    ingest_from_manifest(settings)
    return settings


@pytest.fixture()
def classified(ingested, scripted_model):
    scripted_model()
    run = classify_signals(EXPORT, ingested)
    return ingested, run


# --- direct messages, excluded by construction -------------------------------


def test_the_parser_never_returns_a_direct_message():
    result = read_chat_export(EXPORT_PATH)

    assert result.direct_messages_excluded == 12
    assert len(result.messages) == 78
    assert result.dm_channels == ["dm-sarah-priya"]
    assert all(not m.channel.startswith("dm") for m in result.messages)


def test_the_forbidden_message_ids_never_reach_the_store(ingested):
    forbidden = set(GOLDEN["dm_messages_that_must_be_excluded"])
    assert len(forbidden) == 12

    with database.connect(ingested) as conn:
        stored = {m.external_id for m in chat_repo.list_messages(conn)}

    assert stored, "nothing stored means zero DMs proves nothing"
    assert not (forbidden & stored)


def test_a_direct_message_cannot_be_stored_even_deliberately(ingested):
    """The schema constraint, independent of the parser."""
    conn = sqlite3.connect(ingested.db_path)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="is_direct_message"):
            conn.execute(
                "INSERT INTO chat_messages (id, external_id, source_id, channel, author, ts,"
                " text, is_direct_message)"
                " VALUES ('forced', 'forced', ?, 'dm-x', 'a', '2024-01-01', 'private', 1)",
                (EXPORT,),
            )
    finally:
        conn.close()


def test_a_dm_channel_name_is_enough_without_the_flag():
    """An export that omits the flag must not leak private conversation. That
    is not a mistake that can be undone afterwards."""
    assert is_direct_message({"channel": "dm-sarah-priya", "text": "x"})
    assert is_direct_message({"channel": "direct_thread", "text": "x"})
    assert is_direct_message({"channel": "proj-x", "is_direct_message": True, "text": "x"})
    assert not is_direct_message({"channel": "proj-meridian-dev", "text": "x"})


def test_the_flag_alone_is_enough_without_the_channel_name():
    result = parse_chat_export(json.dumps({"messages": [
        {"id": "m1", "channel": "proj-looks-normal", "author": "a", "text": "private",
         "is_direct_message": True},
        {"id": "m2", "channel": "proj-looks-normal", "author": "a", "text": "public"},
    ]}))

    assert result.direct_messages_excluded == 1
    assert [m.id for m in result.messages] == ["m2"]


# --- ingestion ----------------------------------------------------------------


def test_a_non_consented_export_is_refused_before_it_is_opened(settings):
    outcome = ingest_chat_export(
        SourceMetadata(id="chat-secret", title="Private", source_type="chat_export",
                       consent_flag=False, file_path="chat_export/channels.json"),
        settings=settings,
    )

    assert outcome.source.status is SourceStatus.REFUSED
    assert outcome.report.bytes_read == 0
    with database.connect(settings) as conn:
        assert chat_repo.list_messages(conn, source_id="chat-secret") == []


def test_ingestion_records_what_it_excluded(ingested):
    """The messages leave no trace, so the count is the only evidence they were
    seen at all."""
    from app.db.repositories import sources as source_repo

    with database.connect(ingested) as conn:
        report = source_repo.get_ingestion_report(conn, EXPORT)

    assert report.messages_parsed == 78
    assert report.direct_messages_excluded == 12


def test_a_malformed_export_is_rejected_not_partially_stored(settings, tmp_path):
    path = tmp_path / "broken.json"
    path.write_text('{"messages": [{"id": "m1"},]}', encoding="utf-8")

    outcome = ingest_chat_export(
        SourceMetadata(id="chat-broken", title="Broken", source_type="chat_export", consent_flag=True),
        path, settings=settings,
    )

    assert outcome.source.status is SourceStatus.ERROR
    with database.connect(settings) as conn:
        assert chat_repo.list_messages(conn, source_id="chat-broken") == []


# --- classification -----------------------------------------------------------


def test_classification_refuses_a_non_consented_source(ingested, scripted_model):
    provider = scripted_model()
    with database.transaction(ingested) as conn:
        conn.execute("UPDATE sources SET consent_flag = 0, status = 'refused',"
                     " refusal_reason = 'withdrawn' WHERE id = ?", (EXPORT,))

    with pytest.raises(ConsentRefused):
        classify_signals(EXPORT, ingested)
    assert provider.calls == []


def test_classifying_a_source_with_no_messages_is_a_clean_error(settings, scripted_model):
    scripted_model()
    with pytest.raises(NotFoundError):
        classify_signals("no-such-export", settings)


def test_noise_is_discarded_not_stored(classified):
    """The brief's wording. The schema enforces the same rule from the other
    side: 'noise' is absent from the classification CHECK."""
    settings, run = classified

    assert run.noise_discarded > 0
    with database.connect(settings) as conn:
        stored = chat_repo.list_messages(conn)
        assert all(m.classification is not SignalClass.NOISE for m in stored)
        assert len(stored) == run.classified


def test_every_stored_message_carries_a_class_and_a_confidence(classified):
    settings, _ = classified
    with database.connect(settings) as conn:
        for message in chat_repo.list_messages(conn):
            assert message.classification is not None
            assert message.classification_confidence is not None
            assert message.classified_at is not None


def test_only_signals_that_could_cause_a_write_are_queued(classified):
    """A question produces no downstream write, so queueing one would put work
    in front of a reviewer that has no effect."""
    settings, run = classified

    with database.connect(settings) as conn:
        queued = extraction_repo.list_extractions(conn, extraction_type=ExtractionType.SIGNAL)
        stored = {m.id: m for m in chat_repo.list_messages(conn)}

    assert queued
    assert len(queued) == run.queued
    for extraction in queued:
        label = SignalClass(extraction.payload["classification"])
        assert label in QUEUED_CLASSES

    questions = [m for m in stored.values() if m.classification is SignalClass.QUESTION]
    queued_ids = {e.message_id for e in queued}
    assert questions, "the sample contains questions"
    assert not any(q.id in queued_ids for q in questions)


def test_queued_signals_start_pending_and_link_back_to_the_message(classified):
    """The column points at the stored row, the payload carries the export's
    own id. Both are checked, because they are two different jobs: one is a
    reference, the other is what a person reads."""
    settings, _ = classified
    with database.connect(settings) as conn:
        queued = extraction_repo.list_extractions(conn, extraction_type=ExtractionType.SIGNAL)
        messages = chat_repo.list_messages(conn)
        keys = {m.id for m in messages}
        externals = {m.external_id for m in messages}

    for extraction in queued:
        assert extraction.status is ReviewStatus.PENDING
        assert extraction.message_id in keys
        assert extraction.payload["message_id"] in externals
        assert extraction.verbatim_quote


def test_every_quote_is_a_substring_of_its_own_message(classified):
    """Checked against that message, not against the channel. A quote from a
    different message would verify against the corpus and mislabel this one."""
    from app.ingestion.normaliser import normalise_text

    settings, _ = classified
    with database.connect(settings) as conn:
        queued = extraction_repo.list_extractions(conn, extraction_type=ExtractionType.SIGNAL)
        stored = {m.id: m for m in chat_repo.list_messages(conn)}

    for extraction in queued:
        message = stored[extraction.message_id]
        assert normalise_text(extraction.verbatim_quote) in normalise_text(message.text)


def test_a_model_that_drops_a_message_is_asked_again(ingested, golden_signals):
    """A missing entry is not the same as one labelled noise, and the
    difference changes the precision figure."""
    from app.extraction.llm.factory import set_provider_override
    from app.extraction.llm.fake import FakeProvider
    import re

    calls = {"n": 0}

    def respond(request):
        blocks = re.findall(r"^\[([^\]]+)\] .*?\n\s+(.*?)$", request.prompt, re.M)
        calls["n"] += 1
        if calls["n"] == 1:
            blocks = blocks[:-3]        # drop three, deliberately
        return json.dumps({"signals": [
            {"message_id": mid, "classification": "noise", "quote": " ".join(t.split()),
             "reason": "scripted", "confidence": 0.5}
            for mid, t in blocks
        ]})

    set_provider_override(FakeProvider().default(respond))
    try:
        run = classify_signals(EXPORT, ingested)
    finally:
        set_provider_override(None)

    assert run.ok
    assert calls["n"] > run.batches, "a dropped message must trigger a repair"


def test_a_model_inventing_a_message_id_is_asked_again(ingested):
    from app.extraction.llm.factory import set_provider_override
    from app.extraction.llm.fake import FakeProvider
    import re

    seen = {"repaired": False}

    def respond(request):
        if "were not in the batch" in request.prompt:
            seen["repaired"] = True
            blocks = re.findall(r"^\[([^\]]+)\] .*?\n\s+(.*?)$", request.prompt, re.M)
            return json.dumps({"signals": [
                {"message_id": mid, "classification": "noise", "quote": " ".join(t.split()),
                 "reason": "r", "confidence": 0.5} for mid, t in blocks
            ]})
        return json.dumps({"signals": [
            {"message_id": "msg_9999", "classification": "blocker", "quote": "x",
             "reason": "invented", "confidence": 0.9}
        ]})

    set_provider_override(FakeProvider().default(respond))
    try:
        classify_signals(EXPORT, ingested)
    finally:
        set_provider_override(None)

    assert seen["repaired"], "an unknown message id must be fed back to the model"


# --- two exports, both numbering their messages from one ----------------------


def test_two_exports_with_the_same_message_ids_can_both_be_stored(settings):
    """The bug this namespacing exists for.

    Every export tool numbers its messages from one, so msg_001 in two exports
    is the normal case. Storing the export's own id as the primary key made the
    second upload fail with a UNIQUE violation, found by uploading a second
    export through the interface.
    """
    from app.ingestion.chat_export import ChatMessage

    def message(n: int) -> ChatMessage:
        return ChatMessage(
            id=f"msg_{n:03d}",
            channel="proj-x",
            author="Someone",
            ts=f"2026-09-18T09:{n:02d}:00Z",
            text=f"Message number {n}.",
        )

    with database.transaction(settings) as conn:
        for source_id in ("chat-first", "chat-second"):
            conn.execute(
                "INSERT INTO sources (id, title, source_type, consent_flag, ingested_at, status)"
                " VALUES (?, ?, 'chat_export', 1, '2026-09-18', 'ingested')",
                (source_id, source_id),
            )
            chat_repo.replace_messages(conn, source_id, [message(1), message(2)])

    with database.connect(settings) as conn:
        stored = chat_repo.list_messages(conn)
        first = chat_repo.list_messages(conn, source_id="chat-first")

    assert len(stored) == 4, "both exports are stored in full"
    assert {m.id for m in stored} == {
        "chat-first::msg_001", "chat-first::msg_002",
        "chat-second::msg_001", "chat-second::msg_002",
    }
    assert {m.external_id for m in first} == {"msg_001", "msg_002"}, "the export's own ids survive"


def test_re_uploading_one_export_replaces_only_its_own_messages(settings):
    from app.ingestion.chat_export import ChatMessage

    def message(n: int, text: str) -> ChatMessage:
        return ChatMessage(id=f"msg_{n:03d}", channel="proj-x", author="A", ts="2026-09-18T09:00:00Z", text=text)

    with database.transaction(settings) as conn:
        for source_id in ("chat-a", "chat-b"):
            conn.execute(
                "INSERT INTO sources (id, title, source_type, consent_flag, ingested_at, status)"
                " VALUES (?, ?, 'chat_export', 1, '2026-09-18', 'ingested')",
                (source_id, source_id),
            )
        chat_repo.replace_messages(conn, "chat-a", [message(1, "original a")])
        chat_repo.replace_messages(conn, "chat-b", [message(1, "original b")])
        chat_repo.replace_messages(conn, "chat-a", [message(1, "revised a")])

    with database.connect(settings) as conn:
        texts = {m.source_id: m.text for m in chat_repo.list_messages(conn)}

    assert texts == {"chat-a": "revised a", "chat-b": "original b"}
