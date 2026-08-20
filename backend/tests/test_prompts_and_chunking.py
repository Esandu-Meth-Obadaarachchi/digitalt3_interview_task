"""Prompt versioning and chunking.

Both are things the brief says will be asked about: prompts must be versioned
files rather than inline strings, and the chunking strategy must be documented
and defensible.
"""

from __future__ import annotations

import pytest

from app.db import database
from app.db.repositories import segments as segment_repo
from app.db.repositories import sources as source_repo
from app.extraction.chunker import UNLABELLED, chunk_segments, estimate_tokens, render_segment
from app.extraction.prompts import PROMPT_DIR, clear_cache, list_prompts, load_prompt
from app.ingestion.service import ingest_from_manifest

SPRINT = "meeting-sprint-planning-2024-11-18"


@pytest.fixture()
def ingested(settings):
    ingest_from_manifest(settings)
    with database.connect(settings) as conn:
        source = source_repo.get_source(conn, SPRINT)
        segments = segment_repo.list_segments(conn, SPRINT)
    return source, segments


# --- prompts -----------------------------------------------------------------


def test_every_prompt_declares_a_version_and_a_capability():
    prompts = list_prompts()
    assert prompts, "no prompt files found"
    for prompt in prompts:
        assert prompt.version != "0", f"{prompt.name} has no version header"
        assert prompt.capability
        assert prompt.body


def test_the_version_tag_combines_the_declared_version_and_a_body_hash(tmp_path):
    """An edit made without bumping the version still changes the tag, so a
    measured result can never be attributed to a prompt that did not produce it."""
    clear_cache()
    original = (PROMPT_DIR / "extract_actions.txt").read_text(encoding="utf-8")
    before = load_prompt("extract_actions")

    try:
        (PROMPT_DIR / "extract_actions.txt").write_text(original + "\nAn extra line.\n", encoding="utf-8")
        clear_cache()
        after = load_prompt("extract_actions")
    finally:
        (PROMPT_DIR / "extract_actions.txt").write_text(original, encoding="utf-8")
        clear_cache()

    assert after.version == before.version
    assert after.body_sha256 != before.body_sha256
    assert after.version_tag != before.version_tag


def test_a_missing_prompt_names_the_ones_that_exist():
    with pytest.raises(FileNotFoundError, match="extract_actions"):
        load_prompt("does_not_exist")


def test_the_action_prompt_states_the_rules_the_golden_cases_check():
    body = load_prompt("extract_actions").body
    assert "UNSPECIFIED" in body
    assert "character for character" in body
    assert "Never calculate a calendar date" in body
    assert "Never infer an owner" in body


def test_prompts_render_their_placeholders():
    rendered = load_prompt("extract_actions").render(context="CTX-HERE", chunk="CHUNK-HERE")
    assert "CTX-HERE" in rendered and "CHUNK-HERE" in rendered
    assert "{context}" not in rendered


# --- chunking ----------------------------------------------------------------


def test_every_segment_appears_in_at_least_one_chunk(ingested, settings):
    source, segments = ingested
    chunks = chunk_segments(source, segments, settings, max_tokens=900, overlap_tokens=150)

    covered = {sid for chunk in chunks for sid in chunk.segment_ids}
    assert covered == {s.id for s in segments}


def test_chunks_never_split_a_segment(ingested, settings):
    """A segment is one person's turn. Splitting it separates a commitment from
    the words that make it one, and produces a quote that is not a substring of
    any single line."""
    source, segments = ingested
    by_id = {s.id: s for s in segments}

    for chunk in chunk_segments(source, segments, settings, max_tokens=900, overlap_tokens=150):
        for segment_id in chunk.segment_ids:
            assert by_id[segment_id].text in chunk.text


def test_consecutive_chunks_overlap_by_whole_segments(ingested, settings):
    """A commitment is usually made across two turns: somebody asks, somebody
    agrees. Overlap means the pair appears complete in at least one chunk."""
    source, segments = ingested
    chunks = chunk_segments(source, segments, settings, max_tokens=900, overlap_tokens=150)

    assert len(chunks) > 1
    for previous, current in zip(chunks, chunks[1:], strict=False):
        shared = set(previous.segment_ids) & set(current.segment_ids)
        assert shared, "consecutive chunks must overlap"
        assert set(current.overlap_segment_ids) == shared
        assert current.overlap_segment_ids == current.segment_ids[: len(shared)]


def test_a_chunk_stays_within_its_token_budget(ingested, settings):
    source, segments = ingested
    budget = 900
    for chunk in chunk_segments(source, segments, settings, max_tokens=budget, overlap_tokens=150):
        # A single segment longer than the budget is admitted whole rather than
        # split, so the ceiling is the budget or one segment, whichever is larger.
        assert chunk.estimated_tokens <= budget + estimate_tokens(chunk.text.splitlines()[-1])


def test_the_context_header_names_every_participant(ingested, settings):
    """Without the participant list the model cannot tell that "James" is
    James Liu, or that two people called Priya are in the room. Owner
    attribution is golden case 3, and this is where its errors come from."""
    source, segments = ingested
    chunk = chunk_segments(source, segments, settings, max_tokens=900, overlap_tokens=150)[0]

    for participant in source.participants:
        assert participant in chunk.context
    assert source.title in chunk.context
    assert "never quote from this block" in chunk.context


def test_rendered_lines_carry_the_timestamp_and_the_speaker(ingested, settings):
    source, segments = ingested
    chunk = chunk_segments(source, segments, settings, max_tokens=900, overlap_tokens=150)[0]
    first = chunk.text.splitlines()[0]

    assert first.startswith("[00:00:05] Sarah Chen: ")


def test_an_unlabelled_speaker_renders_as_the_same_token_the_model_must_output():
    """The transcript and the prompt agree on what "not stated" looks like."""
    from app.models.source import Segment

    segment = Segment(id="s", source_id="src", segment_index=0, speaker=None,
                      start_ts="00:02:04", text="We'd need to update it.", char_start=0, char_end=23)
    assert render_segment(segment) == f"[00:02:04] {UNLABELLED}: We'd need to update it."
    assert UNLABELLED == "UNSPECIFIED"


def test_an_empty_transcript_produces_no_chunks(ingested, settings):
    source, _ = ingested
    assert chunk_segments(source, [], settings) == []


def test_chunk_metadata_locates_the_window_in_the_source(ingested, settings):
    source, segments = ingested
    chunks = chunk_segments(source, segments, settings, max_tokens=900, overlap_tokens=150)

    assert chunks[0].index == 0
    assert all(c.total == len(chunks) for c in chunks)
    assert chunks[0].char_start == segments[0].char_start
    assert chunks[-1].char_end == segments[-1].char_end
    assert chunks[0].start_ts == "00:00:05"
