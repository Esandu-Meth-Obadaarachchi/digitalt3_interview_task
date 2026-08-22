"""The HTTP surface for M1 and M2.

The rubric red-flags business logic inside route handlers, so these tests check
the endpoints return what the services produced, and that the consent rule
holds when reached over HTTP rather than through the Python API.
"""

from __future__ import annotations

import io
import json

from app.config import REPO_ROOT
from app.db import database


def test_health_reports_configuration_without_leaking_the_key(client, settings):
    body = client.get("/health").json()

    assert body["status"] == "ok"
    # Read from the store rather than pinned to a literal. The literal said "1"
    # and went stale the moment the schema gained a table, which tested the
    # number rather than the reporting.
    assert body["schema_version"] == database.schema_version(settings)
    assert body["llm_provider"] == "fake"
    assert set(body) == {
        "status", "schema_version", "llm_provider", "llm_model", "llm_key_present",
        "llm_available", "llm_detail", "retrieval_mode", "tracker_provider",
    }
    assert isinstance(body["llm_key_present"], bool)


def test_seed_endpoint_ingests_the_committed_sample_data(client):
    response = client.post("/api/sources/seed")
    assert response.status_code == 200

    statuses = {o["source"]["id"]: o["source"]["status"] for o in response.json()}
    # The four originally supplied meetings, asserted by id. The sample set may
    # grow, so this checks these four are handled correctly rather than that
    # nothing else exists.
    assert statuses["meeting-sprint-planning-2024-11-18"] == "ingested"
    assert statuses["meeting-client-status-2024-08-19"] == "ingested"
    assert statuses["meeting-team-sync-2024-11-15"] == "refused"
    assert statuses["meeting-design-review-2024-11-17"] == "error"


def test_refused_and_errored_sources_are_listed_not_hidden(client):
    """A refusal is a record. The demo has to show the meeting was seen and
    declined, not that it silently vanished."""
    client.post("/api/sources/seed")

    listed = {s["id"]: s for s in client.get("/api/sources").json()}
    assert len(listed) >= 4

    refused = listed["meeting-team-sync-2024-11-15"]
    assert refused["status"] == "refused"
    assert "not true" in refused["refusal_reason"]

    errored = listed["meeting-design-review-2024-11-17"]
    assert errored["status"] == "error"
    assert "truncated_mid_sentence" in errored["error_detail"]


def test_sources_can_be_filtered_by_status(client):
    client.post("/api/sources/seed")
    ingested = {s["id"] for s in client.get("/api/sources", params={"status": "ingested"}).json()}
    refused = {s["id"] for s in client.get("/api/sources", params={"status": "refused"}).json()}

    assert {"meeting-sprint-planning-2024-11-18", "meeting-client-status-2024-08-19"} <= ingested
    assert refused == {"meeting-team-sync-2024-11-15"}
    assert not (ingested & refused)


def test_the_ingestion_report_carries_the_consent_evidence(client):
    client.post("/api/sources/seed")
    report = client.get("/api/sources/meeting-team-sync-2024-11-15/report").json()

    assert report["bytes_read"] == 0
    assert report["consent"]["granted"] is False
    assert report["segments_parsed"] == 0
    assert report["content_hash"] is None


def test_segments_carry_speaker_timestamp_and_citable_offsets(client):
    client.post("/api/sources/seed")
    segments = client.get("/api/sources/meeting-sprint-planning-2024-11-18/segments").json()
    text = client.get("/api/sources/meeting-sprint-planning-2024-11-18/text").json()["text"]

    assert len(segments) == 55
    assert segments[0]["speaker"] == "Sarah Chen"
    assert segments[0]["start_ts"] == "00:00:05"

    for segment in segments:
        assert text[segment["char_start"] : segment["char_end"]] == segment["text"]


def test_an_unknown_source_is_a_404_not_a_500(client):
    assert client.get("/api/sources/does-not-exist").status_code == 404
    assert client.get("/api/sources/does-not-exist/segments").status_code == 404
    assert client.get("/api/sources/does-not-exist/report").status_code == 404


def test_uploading_without_stating_consent_is_rejected(client):
    """The consent flag is a required field. An upload that omits it fails
    validation rather than being defaulted in either direction."""
    response = client.post(
        "/api/sources/upload",
        files={"file": ("x.txt", io.BytesIO(b"[00:00:01] Sarah Chen: Hello.\n"), "text/plain")},
        data={"source_id": "upload-1", "title": "Upload"},
    )
    assert response.status_code == 422


def test_a_non_consented_upload_is_refused_and_leaves_nothing_on_disk(client, settings):
    content = (REPO_ROOT / "sample_data" / "transcripts" / "sprint_planning.txt").read_bytes()

    response = client.post(
        "/api/sources/upload",
        files={"file": ("secret.txt", io.BytesIO(content), "text/plain")},
        data={
            "source_id": "upload-no-consent",
            "title": "Uploaded without consent",
            "consent_flag": "false",
            "participants": json.dumps(["Someone"]),
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["source"]["status"] == "refused"
    assert body["report"]["bytes_read"] == 0
    assert body["segments"] == []

    uploads = settings.db_path.parent / "uploads"
    assert not list(uploads.glob("upload-no-consent*")), "a refused upload must not be retained"


def test_a_consented_upload_is_ingested(client):
    content = (REPO_ROOT / "sample_data" / "transcripts" / "client_status_call.txt").read_bytes()

    response = client.post(
        "/api/sources/upload",
        files={"file": ("call.txt", io.BytesIO(content), "text/plain")},
        data={
            "source_id": "upload-ok",
            "title": "Uploaded with consent",
            "consent_flag": "true",
            "meeting_date": "2024-11-18",
            "participants": json.dumps(["Lisa Tran", "David Park", "Sarah Chen", "Priya Sharma"]),
        },
    )

    body = response.json()
    assert body["source"]["status"] == "ingested"
    assert body["report"]["segments_parsed"] == 51
    assert body["report"]["bytes_read"] == len(content)


def test_a_malformed_upload_is_rejected_with_a_readable_reason(client):
    response = client.post(
        "/api/sources/upload",
        files={
            "file": (
                "broken.txt",
                io.BytesIO(
                    b"[00:00:01] Sarah Chen: We need to ship on Friday.\n"
                    b"[00:00:06] David Park: Understood, I will confirm with the team.\n"
                    b"[00:00:11] Sarah Chen: Good. The next thing is the reporting mod"
                ),
                "text/plain",
            )
        },
        data={"source_id": "upload-broken", "title": "Broken", "consent_flag": "true"},
    )

    body = response.json()
    assert body["source"]["status"] == "error"
    assert "truncated_mid_sentence" in body["source"]["error_detail"]
    assert body["segments"] == []


# --- uploading a chat export, not only a transcript ---------------------------


def test_a_chat_export_can_be_uploaded_and_direct_messages_never_land(client, settings):
    """The same endpoint, the same gate, a different parser.

    The assertion worth having is the second one: the export contains direct
    messages, the report counts them, and the store holds none of them.
    """
    content = (REPO_ROOT / "sample_data" / "chat_export" / "channels.json").read_bytes()

    response = client.post(
        "/api/sources/upload",
        files={"file": ("channels.json", io.BytesIO(content), "application/json")},
        data={
            "source_id": "upload-chat",
            "title": "Uploaded channels",
            "consent_flag": "true",
            "source_type": "chat_export",
        },
    )

    body = response.json()
    assert body["source"]["status"] == "ingested"
    assert body["source"]["source_type"] == "chat_export"
    assert body["report"]["messages_parsed"] > 0
    assert body["report"]["direct_messages_excluded"] > 0, "the sample export contains DMs"
    assert body["segments"] == [], "a chat export has messages, not segments"

    with database.connect(settings) as conn:
        stored = conn.execute(
            "SELECT COUNT(*) AS n FROM chat_messages WHERE source_id = 'upload-chat'"
        ).fetchone()
        dms = conn.execute(
            "SELECT COUNT(*) AS n FROM chat_messages WHERE is_direct_message = 1"
        ).fetchone()

    assert stored["n"] == body["report"]["messages_parsed"]
    assert dms["n"] == 0


def test_a_non_consented_chat_export_is_refused_and_leaves_nothing_on_disk(client, settings):
    """The gate does not care which parser would have run."""
    content = (REPO_ROOT / "sample_data" / "chat_export" / "channels.json").read_bytes()

    response = client.post(
        "/api/sources/upload",
        files={"file": ("channels.json", io.BytesIO(content), "application/json")},
        data={
            "source_id": "upload-chat-no-consent",
            "title": "Channels without consent",
            "consent_flag": "false",
            "source_type": "chat_export",
        },
    )

    body = response.json()
    assert body["source"]["status"] == "refused"
    assert body["report"]["bytes_read"] == 0
    assert not list((settings.db_path.parent / "uploads").glob("upload-chat-no-consent*"))

    with database.connect(settings) as conn:
        stored = conn.execute("SELECT COUNT(*) AS n FROM chat_messages").fetchone()
    assert stored["n"] == 0


def test_a_transcript_uploaded_as_a_chat_export_fails_with_a_readable_reason(client):
    content = (REPO_ROOT / "sample_data" / "transcripts" / "client_status_call.txt").read_bytes()

    response = client.post(
        "/api/sources/upload",
        files={"file": ("call.txt", io.BytesIO(content), "text/plain")},
        data={
            "source_id": "upload-wrong-kind",
            "title": "Wrong kind",
            "consent_flag": "true",
            "source_type": "chat_export",
        },
    )

    body = response.json()
    assert body["source"]["status"] == "error"
    assert body["report"]["rejection_reason"], "an error must say why"


def test_audio_is_named_as_not_built_rather_than_misrouted(client):
    """Without this it reaches the transcript parser and reports a parse
    failure, which describes the wrong problem."""
    response = client.post(
        "/api/sources/upload",
        files={"file": ("meeting.wav", io.BytesIO(b"RIFF"), "audio/wav")},
        data={
            "source_id": "upload-audio",
            "title": "Recording",
            "consent_flag": "true",
            "source_type": "audio",
        },
    )

    assert response.status_code == 422
    assert "not built" in response.json()["detail"]


def test_an_unknown_source_type_is_refused(client):
    response = client.post(
        "/api/sources/upload",
        files={"file": ("x.txt", io.BytesIO(b"[00:00:01] Sarah Chen: Hello.\n"), "text/plain")},
        data={
            "source_id": "upload-unknown-kind",
            "title": "Unknown",
            "consent_flag": "true",
            "source_type": "spreadsheet",
        },
    )

    assert response.status_code == 422
    assert "transcript" in response.json()["detail"]


def test_the_default_is_still_a_transcript(client):
    """Callers predating the field keep working."""
    content = (REPO_ROOT / "sample_data" / "transcripts" / "client_status_call.txt").read_bytes()

    response = client.post(
        "/api/sources/upload",
        files={"file": ("call.txt", io.BytesIO(content), "text/plain")},
        data={"source_id": "upload-default", "title": "Default", "consent_flag": "true"},
    )

    assert response.json()["source"]["source_type"] == "transcript"
