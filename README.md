# Meeting & Channel Intelligence Agent

Turns meeting transcripts and chat exports into traceable, human-approved
records. Every extracted action, decision and risk is anchored to a verbatim
quote from its source. Nothing reaches an external system until a person
approves it.

Built for the DigitalT3 intern selection challenge. The brief is committed at
[`docs/challenge/`](docs/challenge/).

> **Status: Phase 1 of 12 complete.** This README is rewritten from the code at
> the end of every phase. The table below reports what runs today, not what is
> planned. Nothing is marked Done until it runs end to end on the sample data.

---

## Capability status

| ID  | Capability                       | Priority | Status    | Note |
|-----|----------------------------------|----------|-----------|------|
| M1  | Ingest and normalise a source    | MUST     | Partial   | txt, vtt and json transcripts done and tested. Audio transcription at Phase 9 |
| M2  | Consent gate                     | MUST     | Done      | Enforced on metadata before the file is opened, and again by database trigger |
| M3  | Extract action items             | MUST     | Not built | Phase 3 |
| M4  | Extract decisions                | MUST     | Not built | Phase 5 |
| M5  | Extract risks and blockers       | SHOULD   | Not built | Phase 5 |
| M6  | Review and approval queue        | MUST     | Not built | Approval and audit triggers written and tested, no queue yet |
| M7  | Write approved items to tracker  | MUST     | Not built | Idempotency constraint written and tested, no adapter yet |
| M8  | Cross-source question answering  | MUST     | Not built | Phase 6. FTS5 index is already populated at ingestion |
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
backend/app/db/schema.sql            13 tables, 3 FTS5 indexes, 20 triggers
backend/app/config.py                typed configuration from .env
backend/app/errors.py                domain errors, sqlite error translation
backend/app/db/database.py           connection, transaction, init, reset
backend/app/db/repositories/         all SQL, one module per entity
backend/app/models/                  Pydantic contracts
backend/app/ingestion/
  consent.py                         M2, the gate
  reader.py                          bytes, strict UTF-8, encoding defects
  parsers/txt.py vtt.py json_...     M1, three formats, one internal shape
  parsers/speakers.py                is this prefix really a speaker label
  validator.py                       severity-graded defects
  normaliser.py                      segments with citable char offsets
  service.py                         the pipeline, one transaction per source
backend/app/main.py                  FastAPI app, one error-to-status mapping
backend/app/routers/sources.py       thin HTTP surface
backend/tests/                       71 passing tests
scripts/seed.py                      rebuild the store and ingest sample data
scripts/make_format_fixtures.py      generates the vtt/json/defect fixtures
sample_data/                         4 transcripts, 1 chat export, 5 golden files
```

Try it:

```bash
make seed                                     # ingests, refuses and rejects, and says which
curl localhost:8000/api/sources | jq          # after `make run`
curl localhost:8000/api/sources/meeting-team-sync-2024-11-15/report | jq .bytes_read   # 0
```

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

**The consent gate fires on metadata, before the file is opened.** A refused
source's ingestion report carries `bytes_read: 0`, which is machine-checkable
evidence that the content was never read, never parsed and never sent to a
model. Checking after parsing would satisfy the wording while the content sat
in memory and in the store.

**Ambiguity is never resolved by guessing.** An unlabelled transcript line
keeps `speaker: null` and records a warning saying why, rather than inheriting
the speaker from the line above. A first name shared by two participants
(the sample data contains both a Priya Sharma and a Priya Menon) is left
unresolved. `UNSPECIFIED` is a first-class typed value, so the model always
has a correct thing to output.

**Malformed files are graded, not binary.** Truncation, undecodable bytes and
an unrecognisable format reject the whole file and store nothing. A missing
speaker label or a missing timestamp travels with the source as a warning. The
brief requires the deliberately broken sample be rejected with a clear reason;
it does not require rejecting every imperfect file.

**Direct messages are excluded by construction.** `chat_messages` carries
`CHECK (is_direct_message = 0)`, so a DM is physically unstorable rather than
merely filtered by a code path. `noise` is likewise absent from the
classification constraint, because noise is discarded and not stored.

**Raw `sqlite3` rather than an ORM.** The brief requires the schema be visible
in the repo. `schema.sql` is then literally the schema a reviewer reads. FTS5
virtual tables and their sync triggers are native SQL and would need a
hand-written migration under an ORM anyway, and Pydantic already supplies the
typed contracts.

**One definition of "the text of a source".** Segments are joined by single
spaces after whitespace normalisation. Quote verification checks against that
string and every character offset indexes into it, so a citation points at a
location inside a source rather than at the source, and can be checked by hand.

Fuller reasoning, including what has been cut, lives in
[`decision_log.md`](decision_log.md).

## Sample data

Committed under [`sample_data/`](sample_data/). Four transcripts (two valid
working sessions, one with `consent_flag: false`, one deliberately malformed),
one chat export of 90 messages across three channels of which one is a direct
message thread, and five hand-labelled golden files.

The transcript content was generated with an LLM, which the brief states is
expected and fine. The golden labels were checked by hand, and the check is
automated: `test_every_golden_quote_is_a_substring_of_the_normalised_source_text`
asserts every hand-labelled quote verifies against the exact string the
extraction pipeline will use, so ground truth cannot silently drift from the
system that is measured against it.

`sample_data/format_fixtures/` holds the same client status call rendered as
WebVTT and as JSON, plus files with invalid UTF-8, zero bytes and an
unrecognisable format. They exercise the parsers and the read-defect paths.
They are deliberately not registered in `sources.json`: ingesting the same
conversation three times would double-count it and skew every golden metric.

---

## AI assistant usage

Built with Claude Code as the primary coding assistant, used for scaffolding,
implementation and debugging. Every design decision recorded here was made and
is defended by the candidate.
