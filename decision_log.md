# Decision and assumption log

Scope decisions, assumptions about ambiguous requirements, and known
limitations. Appended to as the build proceeds. Newest phase last.

---

## Phase 0 — Foundation

### Decisions

**D1. Python 3.11, not 3.13 or 3.14.**
Both newer versions are installed on the build machine. 3.11 is the newest
release with settled wheels for `faiss-cpu`, `sentence-transformers` and
`ctranslate2` on Apple Silicon. Verified by resolving the full dependency tree
before committing to it, rather than discovering a missing wheel in Phase 6.

**D2. Raw `sqlite3` over SQLAlchemy.**
The brief requires the schema be visible in the repo rather than created ad hoc
at runtime. With raw SQL, `schema.sql` *is* that artefact. The FTS5 virtual
tables, their sync triggers and the consent/approval triggers are native SQL
and would need a hand-written migration under an ORM regardless, so the ORM
would add an abstraction without removing any work. Pydantic already provides
the typed contracts an ORM would otherwise justify.
*Cost:* a future Postgres move would mean rewriting the queries. Acceptable,
since the brief names SQLite as the default choice and multi-tenancy is
explicitly out of scope.

**D3. Gating enforced in the database, not only in the service layer.**
The rubric's red flag for human-in-the-loop gating is "approval exists in the
UI but is bypassable via the API". Enforcing in Python alone leaves the same
weakness one layer down. Three triggers and one unique constraint mean the
consent gate, the approval gate and write idempotency hold even against direct
`sqlite3` CLI access. The service layer repeats the checks so the API returns a
readable error rather than an `IntegrityError`.
*Cost:* the rules exist in two places and could drift. Mitigated by
`backend/tests/test_schema_guarantees.py`, which asserts the database refuses
each forbidden operation directly.

**D4. Direct messages and noise are unstorable, not merely unstored.**
`CHECK (is_direct_message = 0)` on `chat_messages`, and `noise` omitted from
the classification constraint. Golden case 7 requires zero DM records in the
store. A constraint makes that a property of the schema rather than a claim
about a code path.
*Consequence:* the count of excluded direct messages is recorded in
`ingestion_reports`, since the messages themselves leave no trace by design.

**D5. `expired` as a fourth review state.**
The rubric asks for "a safe default on timeout or no response" under
human-in-the-loop gating. A pending extraction older than `PENDING_EXPIRY_HOURS`
is swept to `expired`, which the approval-gate trigger treats exactly like
`pending`: not writable. The safe default is refusal, never an implicit
approval.

**D6. `original_payload` immutable, `payload` editable, status terminal.**
The review surface has to show what the model said beside what the human
changed, so the model's first output is protected by trigger. Approved,
rejected and expired are terminal, which closes the route where a rejected
payload is reopened and pushed through.

**D7. The database is a build artefact, not source.**
`make seed` drops and rebuilds it from `schema.sql`. Seed data is disposable,
so migration tooling would never be exercised and would only add a second place
where the schema is defined.

**D8. `SourceMetadata` accepts field aliases.**
Supplied metadata is an external shape, so the contract accepts `date` or
`meeting_date` and `consent` or `consent_flag`, normalising to one internal
name. The alias list is the only place that mapping lives, which keeps the
external shape out of the rest of the system.

### Assumptions

**A1.** Reviewer identity is a supplied name, not an authenticated user.
Authentication and multi-tenancy are explicitly out of scope in the brief. The
approval audit records whatever name the reviewer supplies. A real deployment
would take this from the session.

**A2.** Consent is a property of a source, not of an individual participant.
The brief supplies one consent flag per source and says nothing about
per-speaker consent, so the system does not model it.

### Known limitations

**L1.** No database migrations. Changing `schema.sql` requires `make seed`,
which discards existing data.

**L2.** Direct dependencies are pinned exactly, transitive ones are not. A full
lock file was judged not worth the tooling for a build of this size.

### Deviations from the supplied build spec

The planning document at `docs/planning/CLAUDE_CODE_BUILD_SPEC.md` proposed
Streamlit and SQLite FTS5 alone. This build uses React for the review surface
and hybrid retrieval (FTS5 plus FAISS over `all-MiniLM-L6-v2`, fused with
Reciprocal Rank Fusion) with all three modes measured side by side by the eval
harness. Rationale for the retrieval change is recorded in Phase 6. The brief
warns that "keyword search that works and is measured beats a vector store that
is never evaluated", which is met by measuring all three rather than by
avoiding the vector store.

---

## Phase 1 — Ingestion and the consent gate

### Decisions

**D9. The consent gate fires on metadata, before the file is opened.**
The capability test requires the non-consented meeting is "never transcribed,
never sent to a model, and produces zero extracted items". Checking after
parsing would satisfy the wording while the content sat in memory and in the
store. The gate therefore runs first, on the metadata alone, and a refused
source never reaches the read step.
*Evidence, not assertion:* a refused source's ingestion report carries
`bytes_read: 0` and `content_hash: null`. That number is the demo's proof.
*Layers:* metadata gate, then a second check in the extraction service before
any model call, then `trg_consent_gate_insert` in the database. Any one
satisfies the requirement. Together, no code path reaches an extraction for a
non-consented source.

**D10. Defects are severity-graded, not binary.**
ERROR-level defects (truncation, undecodable bytes, no parseable segments,
unknown format, malformed structure) reject the file whole and store nothing.
WARNING-level defects (missing speaker label, missing timestamp, non-monotonic
timestamps, empty segment) travel with the source and surface in review.
The brief requires the deliberately malformed sample be rejected with a clear
reason and not corrupt the store. It does not require rejecting every imperfect
file, and a transcript with one unlabelled line is realistic input.

**D11. Truncation detection is a heuristic, and is documented as one.**
The rule: the final segment does not end in terminal punctuation. On the
committed samples this cleanly separates `malformed_meeting.txt` (ends on
"upd") from the three valid transcripts (all end on a full stop).
*False-positive mode:* a recording that genuinely ends mid-thought would be
rejected. Mitigated by making the check switchable per source
(`check_truncation=False`), which is tested.
*Alternative considered:* checking whether the final token is a dictionary
word. Rejected as more machinery for no more certainty.

**D12. An unlabelled line keeps `speaker: null`.**
In `malformed_meeting.txt` three lines carry a timestamp but no speaker, and
each directly follows a line by the same person, so inheriting the speaker
above would look correct almost every time. It would still be a guess, and
rule 1 of the brief forbids inventing a speaker. Each such line records a
`missing_speaker_label` warning stating exactly that.

**D13. A single-token speaker candidate is accepted only against metadata.**
Splitting on the first colon turns "Note: the deadline moved" into a speaker
called Note, which a test caught. Two signals are used instead: the candidate
matches a participant named in the source metadata, or the candidate is
name-shaped (two to four capitalised tokens, no sentence punctuation, first
word not a document-structure word such as note, action, agenda, summary).
A lone capitalised word is too weak a signal on its own.
*Consequence:* a transcript with no participant metadata that writes
"Sarah: ..." loses the speaker label. That is the safe failure. The unsafe
failure is inventing one.

**D14. A first name shared by two participants is left unresolved.**
The sample data plants Priya Sharma and Priya Menon in the same meeting, so a
bare "Priya" is ambiguous. The speaker lookup registers a first name only when
exactly one participant owns it.
*Consequence:* cross-source identity resolution is a separate, evaluated
problem, deferred to the Phase 11 stretch work where the merge rule can be
measured rather than assumed.

**D15. One definition of "the text of a source".**
`source_text = " ".join(whitespace-normalised segment texts, in order)`.
Quote verification checks against that string, and `char_start` / `char_end`
index into it. Because both use one definition, an offset is checkable by hand
and a quote spanning two segments still verifies. Speaker labels and timestamps
are excluded deliberately: a verbatim quote must be words somebody said, not
"[00:02:17] Priya Sharma:".
*Guarded by test:* every hand-labelled golden quote is asserted to be a literal
substring of this exact string, so ground truth cannot drift from the system
measured against it.

**D16. The rejection message names the most diagnostic defect, not the first.**
A file often trips several blocking checks at once. Reporting "only 1 segment
parsed" when the real problem is a truncated recording sends a reader looking
in the wrong place, so blocking defects are ranked and the summary leads with
the most specific.

**D17. Format fixtures are committed but not registered as sources.**
The brief names txt, vtt and json but supplies only .txt, so two parsers would
have been asserted rather than demonstrated. `sample_data/format_fixtures/`
holds the same client status call as WebVTT and as JSON, generated by a
committed script, and the suite proves all three formats normalise to identical
segments. They are absent from `sources.json` on purpose: ingesting the same
conversation three times would double-count it in the extraction corpus and
skew every golden metric.

**D18. Warnings are errors in the test suite.**
One named third-party exception (Starlette's nudge towards httpx2 on importing
its test client). `pytest-asyncio` was removed rather than configured, because
nothing in the suite is asynchronous and an unused dependency is a claim about
the build that is not true.

### Assumptions

**A3.** Transcript timestamps are wall-clock offsets from the start of the
recording, not absolute times. The samples use `[HH:MM:SS]` with no date, and
nothing in the brief suggests otherwise.

**A4.** Re-ingesting a source replaces it. Sources are keyed by a stable id
supplied in metadata, so a second ingest of the same id is a correction rather
than a new meeting.

### Known limitations

**L3.** Speaker diarisation is out of scope per the brief. Speaker labels come
from the source or are absent. Nothing infers who spoke.

**L4.** The truncation heuristic is a heuristic. See D11.

**L5.** Audio ingestion is not built yet. M1 is marked Partial in the README
for that reason, and audio arrives in Phase 9.
