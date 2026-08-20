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
