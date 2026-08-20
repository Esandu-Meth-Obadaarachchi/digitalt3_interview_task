# Meeting & Channel Intelligence Agent

Turns meeting transcripts and chat exports into traceable, human-approved
records. Every extracted action, decision and risk is anchored to a verbatim
quote from its source. Nothing reaches an external system until a person
approves it.

Built for the DigitalT3 intern selection challenge. The brief is committed at
[`docs/challenge/`](docs/challenge/).

> **Status: Phase 0 of 12 complete.** This README is rewritten from the code at
> the end of every phase. The table below reports what runs today, not what is
> planned. Nothing is marked Done until it runs end to end on the sample data.

---

## Capability status

| ID  | Capability                       | Priority | Status    | Note |
|-----|----------------------------------|----------|-----------|------|
| M1  | Ingest and normalise a source    | MUST     | Not built | Schema and contracts in place, parsers arrive in Phase 1 |
| M2  | Consent gate                     | MUST     | Not built | Database trigger enforcing it is written and tested, no ingestion path to gate yet |
| M3  | Extract action items             | MUST     | Not built | Phase 3 |
| M4  | Extract decisions                | MUST     | Not built | Phase 5 |
| M5  | Extract risks and blockers       | SHOULD   | Not built | Phase 5 |
| M6  | Review and approval queue        | MUST     | Not built | Approval and audit triggers written and tested, no queue yet |
| M7  | Write approved items to tracker  | MUST     | Not built | Idempotency constraint written and tested, no adapter yet |
| M8  | Cross-source question answering  | MUST     | Not built | Phase 6 |
| M9  | Chat signal classification       | SHOULD   | Not built | DM exclusion enforced by schema, no parser yet |
| M10 | Scheduled end-of-day digest      | SHOULD   | Not built | Phase 8 |
| M11 | Structured outcome record        | SHOULD   | Not built | Phase 8 |
| M12 | Follow-up message draft          | COULD    | Not built | Phase 8 |
| M13 | Per-person digest                | COULD    | Not built | Phase 8 |

**Evaluation results:** none yet. The harness arrives in Phase 3. No number
appears in this README until `make eval` reproduces it.

---

## What exists right now

```
backend/app/db/schema.sql        13 tables, 3 FTS5 indexes, 20 triggers
backend/app/config.py            typed configuration from .env
backend/app/errors.py            domain errors, sqlite error translation
backend/app/db/database.py       connection, transaction, init, reset
backend/app/models/              Pydantic contracts for sources and segments
backend/tests/                   19 passing tests over the schema guarantees
scripts/seed.py                  rebuild the store, validate the sample manifest
sample_data/                     4 transcripts, 1 chat export, 5 golden files
Makefile                         setup / seed / run / test / eval
```

---

## Setup

Requires Python 3.11 (newest version with settled wheels for `faiss-cpu`,
`sentence-transformers` and `ctranslate2` on Apple Silicon) and Node 20+.

```bash
cp .env.example .env          # then add GEMINI_API_KEY
make setup                    # virtualenv + dependencies
make seed                     # rebuild the store from schema.sql
make test                     # run the test suite
make check-env                # show which providers are configured
```

`make run` starts the API. `make eval` runs the golden cases. Both report
honestly that they are not built yet rather than pretending.

---

## Design decisions made so far

**The consent gate, the approval gate and write idempotency are enforced in
the database, not only in Python.** The rubric asks whether gating is enforced
in code or by convention, and flags "approval exists in the UI but is
bypassable via the API" as a failure. Three SQL triggers and one unique
constraint mean the rules hold against the API, against the service layer, and
against someone opening the file with the `sqlite3` CLI. The service layer
repeats the same checks to return friendly errors. See
[`backend/app/db/schema.sql`](backend/app/db/schema.sql).

**Direct messages are excluded by construction.** `chat_messages` carries
`CHECK (is_direct_message = 0)`, so a DM is physically unstorable rather than
merely filtered by a code path. `noise` is likewise absent from the
classification constraint, because noise is discarded and not stored.

**Raw `sqlite3` rather than an ORM.** The brief requires the schema be visible
in the repo. `schema.sql` is then literally the schema a reviewer reads. FTS5
virtual tables and their sync triggers are native SQL and would need a
hand-written migration under an ORM anyway, and Pydantic already supplies the
typed contracts.

**`UNSPECIFIED` is a first-class value.** The brief names silent guessing as
the most damaging failure mode in this domain. Abstention is an explicit typed
value, so the model always has a correct thing to output and is never cornered
into inventing an owner or a date.

**The database is a build artefact.** `make seed` rebuilds it from
`schema.sql`. Seed data is disposable, so migration tooling that would never be
exercised is not carried.

Fuller reasoning, including what has been cut, lives in
[`decision_log.md`](decision_log.md).

---

## Sample data

Committed under [`sample_data/`](sample_data/). Four transcripts (two valid
working sessions, one with `consent_flag: false`, one deliberately malformed),
one chat export of 90 messages across three channels of which one is a direct
message thread, and five hand-labelled golden files.

The transcript content was generated with an LLM, which the brief states is
expected and fine. The golden labels were checked by hand: all 19 verbatim
quotes in the golden files verify as literal substrings of their transcripts.

---

## AI assistant usage

Built with Claude Code as the primary coding assistant, used for scaffolding,
implementation and debugging. Every design decision recorded here was made and
is defended by the candidate.
