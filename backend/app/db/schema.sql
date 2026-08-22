-- =============================================================================
-- Meeting & Channel Intelligence Agent - authoritative SQLite schema
-- =============================================================================
-- This file is the single definition of the store. There is no migration
-- tool and no ad-hoc CREATE TABLE anywhere in the application code:
-- `make seed` drops the database and rebuilds it from this file plus
-- sample_data/. For a seven-day build with disposable seed data that is
-- simpler than migrations and keeps the schema readable in one place.
--
-- Three of the challenge's behavioural rules are enforced *here*, in the
-- database, and not only in Python:
--
--   M2  consent gate      trg_consent_gate_*      an extraction cannot be
--                                                 inserted against a source
--                                                 whose consent_flag is not 1
--   M6  approval gate     trg_approval_gate_write a tracker write cannot be
--                                                 inserted unless the backing
--                                                 extraction is 'approved'
--   M7  idempotency       tracker_writes.UNIQUE   re-approving the same
--                                                 extraction cannot create a
--                                                 second tracker item
--
-- The service layer checks the same three rules and raises friendly errors.
-- The triggers are the backstop: they hold even if someone bypasses the API
-- and writes to the file with the sqlite3 CLI. Defence in depth, because the
-- rubric asks whether gating is "enforced in code rather than by convention".
-- =============================================================================

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

-- =============================================================================
-- SOURCES  (M1, M2)
-- =============================================================================
-- One row per ingested artefact: a transcript file, an audio recording, or a
-- chat export. status records the outcome of ingestion so a refusal is a
-- first-class stored fact rather than a log line that scrolled away.
CREATE TABLE IF NOT EXISTS sources (
    id              TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    source_type     TEXT NOT NULL CHECK (source_type IN ('transcript', 'audio', 'chat_export')),
    meeting_date    TEXT,                                  -- ISO-8601 date of the meeting itself
    participants    TEXT NOT NULL DEFAULT '[]',            -- JSON array of names
    consent_flag    INTEGER NOT NULL CHECK (consent_flag IN (0, 1)),
    origin_format   TEXT,                                  -- txt | vtt | json | wav | ...
    file_path       TEXT,
    content_hash    TEXT,                                  -- sha256 of raw bytes, gives a stable id
    ingested_at     TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('ingested', 'refused', 'error')),
    refusal_reason  TEXT,                                  -- populated when status = 'refused'
    error_detail    TEXT                                   -- populated when status = 'error'
);

CREATE INDEX IF NOT EXISTS idx_sources_status ON sources (status);
CREATE INDEX IF NOT EXISTS idx_sources_type   ON sources (source_type);

-- A refused or errored source must say why. Prevents a silent refusal that
-- nobody can explain three weeks later.
CREATE TRIGGER IF NOT EXISTS trg_sources_reason_required_insert
BEFORE INSERT ON sources
FOR EACH ROW
WHEN (NEW.status = 'refused' AND (NEW.refusal_reason IS NULL OR NEW.refusal_reason = ''))
   OR (NEW.status = 'error'   AND (NEW.error_detail  IS NULL OR NEW.error_detail  = ''))
BEGIN
    SELECT RAISE(ABORT, 'sources: status refused/error requires a stated reason');
END;

-- =============================================================================
-- INGESTION REPORTS  (M1, M9)
-- =============================================================================
-- What ingestion actually did: segments parsed, lines skipped, warnings,
-- direct messages excluded. This is how the demo proves DM exclusion, since
-- excluded messages are never stored anywhere by design.
CREATE TABLE IF NOT EXISTS ingestion_reports (
    id          TEXT PRIMARY KEY,
    source_id   TEXT NOT NULL REFERENCES sources (id) ON DELETE CASCADE,
    report      TEXT NOT NULL,                             -- JSON
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ingestion_reports_source ON ingestion_reports (source_id);

-- =============================================================================
-- SEGMENTS  (M1)
-- =============================================================================
-- A transcript normalised to ordered lines. char_start/char_end are offsets
-- into the normalised full text of the source, so a citation points at a
-- location *within* the document rather than at the document.
CREATE TABLE IF NOT EXISTS segments (
    id              TEXT PRIMARY KEY,
    source_id       TEXT NOT NULL REFERENCES sources (id) ON DELETE CASCADE,
    segment_index   INTEGER NOT NULL,
    speaker         TEXT,                                  -- NULL when the source did not label it
    start_ts        TEXT,                                  -- HH:MM:SS as it appears in the source
    end_ts          TEXT,
    start_seconds   REAL,                                  -- parsed form, for ordering and audio seek
    text            TEXT NOT NULL,
    char_start      INTEGER NOT NULL,
    char_end        INTEGER NOT NULL,
    UNIQUE (source_id, segment_index)
);

CREATE INDEX IF NOT EXISTS idx_segments_source  ON segments (source_id, segment_index);
CREATE INDEX IF NOT EXISTS idx_segments_speaker ON segments (speaker);

-- -----------------------------------------------------------------------------
-- FTS5 over segments (M8, keyword half of hybrid retrieval)
-- -----------------------------------------------------------------------------
-- External-content table: the index stores no copy of the text, it points at
-- segments by rowid. The three triggers below keep it in step. Porter stemming
-- so "deferred" matches "defer".
CREATE VIRTUAL TABLE IF NOT EXISTS segments_fts USING fts5 (
    text,
    content      = 'segments',
    content_rowid = 'rowid',
    tokenize     = 'porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS trg_segments_fts_insert
AFTER INSERT ON segments
BEGIN
    INSERT INTO segments_fts (rowid, text) VALUES (NEW.rowid, NEW.text);
END;

CREATE TRIGGER IF NOT EXISTS trg_segments_fts_delete
AFTER DELETE ON segments
BEGIN
    INSERT INTO segments_fts (segments_fts, rowid, text) VALUES ('delete', OLD.rowid, OLD.text);
END;

CREATE TRIGGER IF NOT EXISTS trg_segments_fts_update
AFTER UPDATE ON segments
BEGIN
    INSERT INTO segments_fts (segments_fts, rowid, text) VALUES ('delete', OLD.rowid, OLD.text);
    INSERT INTO segments_fts (rowid, text) VALUES (NEW.rowid, NEW.text);
END;

-- =============================================================================
-- CHAT MESSAGES  (M9)
-- =============================================================================
-- Direct messages are excluded *by construction*: the CHECK constraint makes
-- a DM physically unstorable. The parser filters them out and the database
-- refuses them, so "zero DM records entered the store" is a property of the
-- schema rather than a promise about the code path.
--
-- 'noise' is likewise absent from the classification CHECK, because noise is
-- discarded and not stored. An unclassified message carries NULL.
-- `id` is namespaced by source, the same convention segments use, because a
-- message id is only unique inside the export it came from. Two exports both
-- numbering their messages msg_001 is the normal case, not a corner case, and
-- storing the export's own id as the primary key made the second upload fail
-- with a UNIQUE violation. `external_id` keeps the export's own identifier, so
-- a stored message can still be traced back to the system it came from.
CREATE TABLE IF NOT EXISTS chat_messages (
    id                        TEXT PRIMARY KEY,          -- source_id::msg_001
    external_id               TEXT NOT NULL,             -- msg_001, as the export wrote it
    source_id                 TEXT NOT NULL REFERENCES sources (id) ON DELETE CASCADE,
    channel                   TEXT NOT NULL,
    author                    TEXT NOT NULL,
    ts                        TEXT NOT NULL,
    thread_id                 TEXT,
    text                      TEXT NOT NULL,
    is_direct_message         INTEGER NOT NULL DEFAULT 0 CHECK (is_direct_message = 0),
    classification            TEXT CHECK (classification IN ('decision', 'blocker', 'question', 'request')),
    classification_confidence REAL,
    classified_at             TEXT,
    UNIQUE (source_id, external_id)
);

CREATE INDEX IF NOT EXISTS idx_chat_channel ON chat_messages (channel, ts);
CREATE INDEX IF NOT EXISTS idx_chat_class   ON chat_messages (classification);

CREATE VIRTUAL TABLE IF NOT EXISTS chat_messages_fts USING fts5 (
    text,
    content      = 'chat_messages',
    content_rowid = 'rowid',
    tokenize     = 'porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS trg_chat_fts_insert
AFTER INSERT ON chat_messages
BEGIN
    INSERT INTO chat_messages_fts (rowid, text) VALUES (NEW.rowid, NEW.text);
END;

CREATE TRIGGER IF NOT EXISTS trg_chat_fts_delete
AFTER DELETE ON chat_messages
BEGIN
    INSERT INTO chat_messages_fts (chat_messages_fts, rowid, text) VALUES ('delete', OLD.rowid, OLD.text);
END;

CREATE TRIGGER IF NOT EXISTS trg_chat_fts_update
AFTER UPDATE ON chat_messages
BEGIN
    INSERT INTO chat_messages_fts (chat_messages_fts, rowid, text) VALUES ('delete', OLD.rowid, OLD.text);
    INSERT INTO chat_messages_fts (rowid, text) VALUES (NEW.rowid, NEW.text);
END;

-- =============================================================================
-- EXTRACTIONS  (M3, M4, M5, M9)  - proposals as first-class rows
-- =============================================================================
-- Every model output lands here as 'pending'. Nothing downstream reads a row
-- in any other state.
--
--   payload           current JSON, reflects any human edit
--   original_payload  the model's first output, never overwritten
--   status            pending -> approved | rejected | expired
--   expires_at        safe default: an unreviewed item ages out to 'expired'
--                     and 'expired' is not writable
--   model_name /      recorded per row so the eval harness measures a specific
--   prompt_version    model against a specific prompt version, not "the code"
CREATE TABLE IF NOT EXISTS extractions (
    id                TEXT PRIMARY KEY,
    source_id         TEXT NOT NULL REFERENCES sources (id) ON DELETE CASCADE,
    extraction_type   TEXT NOT NULL CHECK (extraction_type IN ('action', 'decision', 'risk', 'signal')),

    payload           TEXT NOT NULL,                       -- JSON, current
    original_payload  TEXT NOT NULL,                       -- JSON, immutable model output
    search_text       TEXT NOT NULL,                       -- denormalised, feeds FTS + embeddings

    verbatim_quote    TEXT NOT NULL,
    quote_verified    INTEGER NOT NULL DEFAULT 0 CHECK (quote_verified IN (0, 1)),

    speaker           TEXT,
    timestamp         TEXT,
    segment_id        TEXT REFERENCES segments (id) ON DELETE SET NULL,
    message_id        TEXT,                                -- chat signals point at a message instead
    char_start        INTEGER,
    char_end          INTEGER,

    confidence        REAL CHECK (confidence IS NULL OR (confidence >= 0.0 AND confidence <= 1.0)),
    dedup_key         TEXT,                                -- stable hash, cross-chunk deduplication
    chunk_id          TEXT,
    merged_from       TEXT NOT NULL DEFAULT '[]',          -- JSON array of absorbed candidate keys
    merge_reason      TEXT,                                -- why the deduplicator merged them
    provider          TEXT,
    model_name        TEXT,
    prompt_version    TEXT,

    status            TEXT NOT NULL DEFAULT 'pending'
                      CHECK (status IN ('pending', 'approved', 'rejected', 'expired')),
    reviewer          TEXT,
    reviewed_at       TEXT,
    review_note       TEXT,
    expires_at        TEXT,
    created_at        TEXT NOT NULL,

    UNIQUE (source_id, extraction_type, dedup_key)
);

CREATE INDEX IF NOT EXISTS idx_extractions_status ON extractions (status, extraction_type);
CREATE INDEX IF NOT EXISTS idx_extractions_source ON extractions (source_id, extraction_type);
CREATE INDEX IF NOT EXISTS idx_extractions_expiry ON extractions (status, expires_at);

-- -----------------------------------------------------------------------------
-- M2 CONSENT GATE, enforced in the database
-- -----------------------------------------------------------------------------
-- No extraction may exist for a source that did not give consent, and no
-- extraction may be re-pointed at one afterwards.
CREATE TRIGGER IF NOT EXISTS trg_consent_gate_insert
BEFORE INSERT ON extractions
FOR EACH ROW
WHEN (SELECT consent_flag FROM sources WHERE id = NEW.source_id) IS NOT 1
BEGIN
    SELECT RAISE(ABORT, 'consent_gate: source consent_flag is not 1, extraction refused');
END;

CREATE TRIGGER IF NOT EXISTS trg_consent_gate_update
BEFORE UPDATE OF source_id ON extractions
FOR EACH ROW
WHEN (SELECT consent_flag FROM sources WHERE id = NEW.source_id) IS NOT 1
BEGIN
    SELECT RAISE(ABORT, 'consent_gate: source consent_flag is not 1, extraction refused');
END;

-- A reviewed row must name its reviewer and when. Keeps the approval audit
-- honest even against a direct UPDATE.
CREATE TRIGGER IF NOT EXISTS trg_extractions_review_audit
BEFORE UPDATE OF status ON extractions
FOR EACH ROW
WHEN NEW.status IN ('approved', 'rejected')
 AND (NEW.reviewer IS NULL OR NEW.reviewer = '' OR NEW.reviewed_at IS NULL)
BEGIN
    SELECT RAISE(ABORT, 'review_audit: approving or rejecting requires reviewer and reviewed_at');
END;

-- A terminal state is terminal. Reopening a rejected or expired item would
-- let an unapproved payload reach the tracker by a side door.
CREATE TRIGGER IF NOT EXISTS trg_extractions_terminal_status
BEFORE UPDATE OF status ON extractions
FOR EACH ROW
WHEN OLD.status IN ('approved', 'rejected', 'expired') AND NEW.status <> OLD.status
BEGIN
    SELECT RAISE(ABORT, 'review_state: status is terminal and cannot be changed once set');
END;

-- The model's original output is evidence. Editing it would destroy the
-- before/after comparison the review surface is built on.
CREATE TRIGGER IF NOT EXISTS trg_extractions_original_immutable
BEFORE UPDATE OF original_payload ON extractions
FOR EACH ROW
WHEN NEW.original_payload <> OLD.original_payload
BEGIN
    SELECT RAISE(ABORT, 'audit: original_payload is immutable');
END;

-- FTS over the denormalised extraction text, so Q&A searches approved
-- decisions and actions as well as raw transcript lines.
CREATE VIRTUAL TABLE IF NOT EXISTS extractions_fts USING fts5 (
    search_text,
    content      = 'extractions',
    content_rowid = 'rowid',
    tokenize     = 'porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS trg_extractions_fts_insert
AFTER INSERT ON extractions
BEGIN
    INSERT INTO extractions_fts (rowid, search_text) VALUES (NEW.rowid, NEW.search_text);
END;

CREATE TRIGGER IF NOT EXISTS trg_extractions_fts_delete
AFTER DELETE ON extractions
BEGIN
    INSERT INTO extractions_fts (extractions_fts, rowid, search_text) VALUES ('delete', OLD.rowid, OLD.search_text);
END;

CREATE TRIGGER IF NOT EXISTS trg_extractions_fts_update
AFTER UPDATE ON extractions
BEGIN
    INSERT INTO extractions_fts (extractions_fts, rowid, search_text) VALUES ('delete', OLD.rowid, OLD.search_text);
    INSERT INTO extractions_fts (rowid, search_text) VALUES (NEW.rowid, NEW.search_text);
END;

-- =============================================================================
-- REVIEW EVENTS  (M6)  - append-only audit trail
-- =============================================================================
-- Every state change writes a row here and rows are never updated or deleted.
-- "Who approved what, when, and what did they change" is answerable from this
-- table alone.
CREATE TABLE IF NOT EXISTS review_events (
    id              TEXT PRIMARY KEY,
    extraction_id   TEXT NOT NULL REFERENCES extractions (id) ON DELETE CASCADE,
    event_type      TEXT NOT NULL CHECK (event_type IN ('created', 'edited', 'approved', 'rejected', 'expired')),
    actor           TEXT NOT NULL,                         -- reviewer name, or 'system' for the expiry sweep
    status_before   TEXT,
    status_after    TEXT,
    payload_before  TEXT,                                  -- JSON, NULL on 'created'
    payload_after   TEXT,                                  -- JSON
    note            TEXT,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_review_events_extraction ON review_events (extraction_id, created_at);
CREATE INDEX IF NOT EXISTS idx_review_events_actor      ON review_events (actor, created_at);

CREATE TRIGGER IF NOT EXISTS trg_review_events_no_update
BEFORE UPDATE ON review_events
BEGIN
    SELECT RAISE(ABORT, 'audit: review_events is append-only, rows cannot be updated');
END;

CREATE TRIGGER IF NOT EXISTS trg_review_events_no_delete
BEFORE DELETE ON review_events
BEGIN
    SELECT RAISE(ABORT, 'audit: review_events is append-only, rows cannot be deleted');
END;

-- =============================================================================
-- TRACKER ITEMS  (M7)  - the mock tracker's own store
-- =============================================================================
-- This table stands in for the foreign system. It holds items the agent never
-- created: a seeded backlog with missing assignees, free-text status values,
-- stale due dates and near-duplicates, because the adapter contract requires
-- that "mocks must return realistically messy data" and that "an agent that
-- only works on clean data has not been tested".
--
-- Deliberately separate from tracker_writes. This is what the tracker holds;
-- tracker_writes is our audit of what we put there. Conflating them would mean
-- the agent could not tell its own writes from somebody else's tickets, which
-- is exactly the situation a real integration is in.
CREATE TABLE IF NOT EXISTS tracker_items (
    external_ref  TEXT PRIMARY KEY,                        -- e.g. MOCK-14
    title         TEXT NOT NULL,
    description   TEXT,
    assignee      TEXT,                                    -- often NULL in real trackers
    status        TEXT NOT NULL,                           -- free text on purpose, not an enum
    due_date      TEXT,                                    -- often NULL, sometimes in the past
    labels        TEXT NOT NULL DEFAULT '[]',              -- JSON array
    source_ref    TEXT,                                    -- our extraction id, NULL for seeded items
    seeded        INTEGER NOT NULL DEFAULT 0 CHECK (seeded IN (0, 1)),
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tracker_items_source ON tracker_items (source_ref);
CREATE INDEX IF NOT EXISTS idx_tracker_items_status ON tracker_items (status);

-- =============================================================================
-- TRACKER WRITES  (M7)
-- =============================================================================
-- The mock tracker's item table. One row per approved extraction, ever.
--
--   UNIQUE (extraction_id)  is the idempotency guarantee. Re-approving or
--                           re-running the write path cannot produce a second
--                           item, because the database will not allow it.
CREATE TABLE IF NOT EXISTS tracker_writes (
    id             TEXT PRIMARY KEY,
    extraction_id  TEXT NOT NULL UNIQUE REFERENCES extractions (id) ON DELETE CASCADE,
    external_ref   TEXT NOT NULL UNIQUE,                   -- mock issue key, e.g. MOCK-14
    provider       TEXT NOT NULL,                          -- which adapter wrote it
    write_payload  TEXT NOT NULL,                          -- JSON, exactly what would go to the real system
    item_status    TEXT NOT NULL DEFAULT 'open',
    written_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tracker_writes_status ON tracker_writes (item_status);

-- -----------------------------------------------------------------------------
-- M6 APPROVAL GATE, enforced in the database
-- -----------------------------------------------------------------------------
-- The one rule the challenge says is an automatic failure if broken. It holds
-- against the API, against the service layer, and against the sqlite3 CLI.
CREATE TRIGGER IF NOT EXISTS trg_approval_gate_write
BEFORE INSERT ON tracker_writes
FOR EACH ROW
WHEN (SELECT status FROM extractions WHERE id = NEW.extraction_id) IS NOT 'approved'
BEGIN
    SELECT RAISE(ABORT, 'approval_gate: extraction is not approved, tracker write refused');
END;

-- =============================================================================
-- TRACKER WRITE ATTEMPTS  (M7)  - every attempt, including the refused ones
-- =============================================================================
-- Separate from tracker_writes on purpose. tracker_writes answers "what
-- exists in the tracker". This answers "what did the agent try to do", which
-- is what proves deduplication and proves the gate fired.
CREATE TABLE IF NOT EXISTS tracker_write_attempts (
    id             TEXT PRIMARY KEY,
    extraction_id  TEXT NOT NULL,
    outcome        TEXT NOT NULL CHECK (outcome IN ('created', 'deduplicated', 'blocked')),
    reason         TEXT,
    external_ref   TEXT,
    provider       TEXT NOT NULL,
    attempted_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_write_attempts_extraction ON tracker_write_attempts (extraction_id, attempted_at);
CREATE INDEX IF NOT EXISTS idx_write_attempts_outcome    ON tracker_write_attempts (outcome);

CREATE TRIGGER IF NOT EXISTS trg_write_attempts_no_update
BEFORE UPDATE ON tracker_write_attempts
BEGIN
    SELECT RAISE(ABORT, 'audit: tracker_write_attempts is append-only');
END;

CREATE TRIGGER IF NOT EXISTS trg_write_attempts_no_delete
BEFORE DELETE ON tracker_write_attempts
BEGIN
    SELECT RAISE(ABORT, 'audit: tracker_write_attempts is append-only');
END;

-- =============================================================================
-- EMBEDDING INDEX  (M8, dense half of hybrid retrieval)
-- =============================================================================
-- FAISS stores vectors against integer ids and nothing else. This table is the
-- mapping back to the row a vector came from, plus the model that produced it,
-- so changing the embedding model invalidates the right rows and not the store.
CREATE TABLE IF NOT EXISTS embedding_index (
    faiss_id      INTEGER PRIMARY KEY,
    ref_type      TEXT NOT NULL CHECK (ref_type IN ('segment', 'chat_message', 'extraction')),
    ref_id        TEXT NOT NULL,
    model_name    TEXT NOT NULL,
    text_hash     TEXT NOT NULL,                           -- detects stale vectors after an edit
    created_at    TEXT NOT NULL,
    UNIQUE (ref_type, ref_id, model_name)
);

CREATE INDEX IF NOT EXISTS idx_embedding_ref ON embedding_index (ref_type, ref_id);

-- =============================================================================
-- DIGESTS  (M10, M13)
-- =============================================================================
-- UNIQUE on (scope_type, scope_key, digest_date) makes a scheduled run
-- idempotent: running the 18:00 job twice replaces one digest rather than
-- producing two.
CREATE TABLE IF NOT EXISTS digests (
    id            TEXT PRIMARY KEY,
    scope_type    TEXT NOT NULL CHECK (scope_type IN ('channel', 'person')),
    scope_key     TEXT NOT NULL,                           -- channel name, or person name
    digest_date   TEXT NOT NULL,                           -- ISO date the digest covers
    generated_at  TEXT NOT NULL,
    trigger       TEXT NOT NULL CHECK (trigger IN ('scheduler', 'manual', 'clock_override')),
    content       TEXT NOT NULL,                           -- JSON: moved / attention / to_decide, each cited
    file_path     TEXT,
    UNIQUE (scope_type, scope_key, digest_date)
);

-- =============================================================================
-- OUTCOME RECORDS  (M11)
-- =============================================================================
-- The versioned artefact a downstream delivery agent consumes without ever
-- touching the transcript store. record_version increments per source so an
-- earlier record stays readable after later approvals.
CREATE TABLE IF NOT EXISTS outcome_records (
    id              TEXT PRIMARY KEY,
    source_id       TEXT NOT NULL REFERENCES sources (id) ON DELETE CASCADE,
    schema_version  TEXT NOT NULL,                         -- version of docs/outcome_schema.json
    record_version  INTEGER NOT NULL,                      -- monotonic per source
    consent_flag    INTEGER NOT NULL CHECK (consent_flag IN (0, 1)),
    content         TEXT NOT NULL,                         -- JSON
    file_path       TEXT,
    created_at      TEXT NOT NULL,
    UNIQUE (source_id, record_version)
);

-- =============================================================================
-- LLM CALLS  (robustness evidence + cost/latency instrumentation)
-- =============================================================================
-- One row per attempt, retries included. Gives the retry rate, the cache hit
-- rate, and per-source cost and latency without any extra bookkeeping.
-- One row per ATTEMPT. `id` identifies the attempt, `call_id` groups the
-- attempts belonging to one logical request, so the retry rate is
--     (attempts - distinct call_ids) / attempts
-- rather than something inferred.
CREATE TABLE IF NOT EXISTS llm_calls (
    id                 TEXT PRIMARY KEY,
    call_id            TEXT NOT NULL,
    source_id          TEXT,
    capability         TEXT NOT NULL,                      -- extract_actions | answer_question | ...
    provider           TEXT NOT NULL,
    model              TEXT NOT NULL,
    prompt_version     TEXT,
    attempt            INTEGER NOT NULL DEFAULT 1,
    outcome            TEXT NOT NULL CHECK (outcome IN (
                           'ok', 'parse_error', 'validation_error',
                           'quote_unverified', 'rate_limited', 'timeout', 'error')),
    prompt_tokens      INTEGER,
    completion_tokens  INTEGER,
    latency_ms         INTEGER,
    cache_hit          INTEGER NOT NULL DEFAULT 0 CHECK (cache_hit IN (0, 1)),
    error              TEXT,
    created_at         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_llm_calls_source     ON llm_calls (source_id, capability);
CREATE INDEX IF NOT EXISTS idx_llm_calls_outcome    ON llm_calls (outcome);
CREATE INDEX IF NOT EXISTS idx_llm_calls_call       ON llm_calls (call_id, attempt);

-- =============================================================================
-- FOLLOW-UP DRAFTS  (M12)
-- =============================================================================
-- A recap message built from approved items, for a person to edit and send.
--
-- The capability is one sentence long and the whole of it is about who acts:
-- "Generate a recap email/message from approved items. Human edits and sends.
-- Agent never sends." Drafting is not an external write, because nothing
-- leaves the machine and every line already passed the approval gate. Sending
-- is an external write, and the rules below are what stop the agent doing it.
--
--   generated_body   what the system produced. Immutable, so the human edit is
--                    always separable from the machine text.
--   edited_body      what the person wrote instead. NULL until they touch it.
--   sent_by          the person who sent it. Never a service.
--
-- A row is born a draft and can only reach 'sent' through an UPDATE, which is
-- where the three send rules apply. Inserting a row already marked sent would
-- walk straight past them, so that is refused too.
CREATE TABLE IF NOT EXISTS followup_drafts (
    id              TEXT PRIMARY KEY,
    source_id       TEXT NOT NULL REFERENCES sources (id) ON DELETE CASCADE,
    draft_version   INTEGER NOT NULL,                      -- monotonic per source
    subject         TEXT NOT NULL,
    generated_body  TEXT NOT NULL,                         -- immutable, see trigger below
    edited_body     TEXT,                                  -- what the human wrote instead
    edited_by       TEXT,
    edited_at       TEXT,
    status          TEXT NOT NULL CHECK (status IN ('draft', 'edited', 'sent')),
    item_count      INTEGER NOT NULL,                      -- approved items it was built from
    channel         TEXT,                                  -- where a person chose to send it
    sent_by         TEXT,                                  -- a person. never a service
    sent_at         TEXT,
    notification_id TEXT,                                  -- the notifier's record of the send
    generated_at    TEXT NOT NULL,
    UNIQUE (source_id, draft_version)
);

CREATE INDEX IF NOT EXISTS idx_followup_source ON followup_drafts (source_id, draft_version);
CREATE INDEX IF NOT EXISTS idx_followup_status ON followup_drafts (status);

-- Every draft starts as a draft. Without this an INSERT could create a row
-- already marked sent and never meet the three rules below, all of which are
-- written on UPDATE.
CREATE TRIGGER IF NOT EXISTS trg_followup_insert_is_draft
BEFORE INSERT ON followup_drafts
FOR EACH ROW
WHEN NEW.status IS NOT 'draft'
BEGIN
    SELECT RAISE(ABORT, 'followup: a draft is created as a draft, not as sent');
END;

-- What the system wrote stays readable next to what the person sent. Same rule
-- as original_payload on extractions, and for the same reason: the interesting
-- question in review is what the human changed.
CREATE TRIGGER IF NOT EXISTS trg_followup_generated_body_immutable
BEFORE UPDATE ON followup_drafts
FOR EACH ROW
WHEN NEW.generated_body IS NOT OLD.generated_body
BEGIN
    SELECT RAISE(ABORT, 'followup: generated_body is immutable, edits belong in edited_body');
END;

-- A send with nobody attached to it is the agent sending. Refused.
CREATE TRIGGER IF NOT EXISTS trg_followup_send_requires_person
BEFORE UPDATE ON followup_drafts
FOR EACH ROW
WHEN NEW.status = 'sent' AND (NEW.sent_by IS NULL OR trim(NEW.sent_by) = '')
BEGIN
    SELECT RAISE(ABORT, 'followup: sending requires the person who sent it. The agent never sends');
END;

-- And naming a service instead of a person is the same thing with a label on
-- it. The capability says the agent never sends, so the database says so too.
CREATE TRIGGER IF NOT EXISTS trg_followup_agent_cannot_send
BEFORE UPDATE ON followup_drafts
FOR EACH ROW
WHEN NEW.status = 'sent'
 AND lower(trim(NEW.sent_by)) IN ('agent', 'system', 'scheduler', 'bot', 'service', 'llm', 'model')
BEGIN
    SELECT RAISE(ABORT, 'followup: sent_by names a service, not a person. The agent never sends');
END;

-- Sent is terminal. A message a person has sent cannot be rewritten afterwards
-- to say something they did not send.
CREATE TRIGGER IF NOT EXISTS trg_followup_sent_is_final
BEFORE UPDATE ON followup_drafts
FOR EACH ROW
WHEN OLD.status = 'sent'
BEGIN
    SELECT RAISE(ABORT, 'followup: this draft was already sent and cannot be changed');
END;

-- =============================================================================
-- SCHEMA VERSION
-- =============================================================================
CREATE TABLE IF NOT EXISTS schema_meta (
    key    TEXT PRIMARY KEY,
    value  TEXT NOT NULL
);

INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('schema_version', '3');
