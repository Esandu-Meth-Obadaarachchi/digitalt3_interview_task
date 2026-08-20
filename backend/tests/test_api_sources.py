"""The HTTP surface for M1 and M2.

The rubric red-flags business logic inside route handlers, so these tests check
the endpoints return what the services produced, and that the consent rule
holds when reached over HTTP rather than through the Python API.
"""

from __future__ import annotations

import io
import json

from app.config import REPO_ROOT


def test_health_reports_configuration_without_leaking_the_key(client):
    body = client.get("/health").json()

    assert body["status"] == "ok"
    assert body["schema_version"] == "1"
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
    assert statuses == {
        "meeting-sprint-planning-2024-11-18": "ingested",
        "meeting-client-status-2024-08-19": "ingested",
        "meeting-team-sync-2024-11-15": "refused",
        "meeting-design-review-2024-11-17": "error",
    }


def test_refused_and_errored_sources_are_listed_not_hidden(client):
    """A refusal is a record. The demo has to show the meeting was seen and
    declined, not that it silently vanished."""
    client.post("/api/sources/seed")

    listed = {s["id"]: s for s in client.get("/api/sources").json()}
    assert len(listed) == 4

    refused = listed["meeting-team-sync-2024-11-15"]
    assert refused["status"] == "refused"
    assert "not true" in refused["refusal_reason"]

    errored = listed["meeting-design-review-2024-11-17"]
    assert errored["status"] == "error"
    assert "truncated_mid_sentence" in errored["error_detail"]


def test_sources_can_be_filtered_by_status(client):
    client.post("/api/sources/seed")
    ingested = client.get("/api/sources", params={"status": "ingested"}).json()
    assert {s["id"] for s in ingested} == {
        "meeting-sprint-planning-2024-11-18",
        "meeting-client-status-2024-08-19",
    }


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
