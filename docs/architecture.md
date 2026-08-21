# Architecture

A required submission deliverable: components, data flow, where the human
approval gate sits, and which adapters exist and what they mock.

---

## One paragraph

A meeting transcript or a chat export is ingested and normalised. A consent gate
refuses anything that did not explicitly permit processing, before the file is
opened. What survives is chunked and sent to a language model, which returns
structured records that are validated against a schema and checked so that every
quote is a literal substring of the source. Every record lands in a review queue
as a **proposal**. Nothing reaches an external system until a human approves it.
Approved records are written to a mock tracker, indexed for question answering,
rolled into scheduled digests, and emitted as versioned outcome records that a
downstream agent can consume without this application.

---

## The data flow

```
  a transcript file                            a chat export
        │                                            │
        ▼                                            ▼
  ┌─────────────────┐                        ┌─────────────────┐
  │  M2 CONSENT     │ ◄── refuses BEFORE ──► │  M2 CONSENT     │
  │     GATE        │     the file is        │     GATE        │
  └────────┬────────┘     opened             └────────┬────────┘
           │  bytes_read: 0 on refusal                │
           ▼                                          ▼
  ┌─────────────────┐                        ┌─────────────────┐
  │  M1 PARSE       │  txt · vtt · json      │  M9 PARSE       │
  │  validate       │  severity-graded       │  DMs dropped    │
  │  normalise      │  defects               │  by construction│
  └────────┬────────┘                        └────────┬────────┘
           │  segments, with char offsets             │  messages
           ▼                                          ▼
  ┌─────────────────┐                        ┌─────────────────┐
  │  CHUNK          │  whole segments        │  BATCH          │
  │  + overlap      │  + context header      │  20 per channel │
  └────────┬────────┘                        └────────┬────────┘
           │                                          │
           └──────────────┬───────────────────────────┘
                          ▼
                 ┌──────────────────┐
                 │   LLM WRAPPER    │   one function, every call
                 │  schema-constrain│   ├─ Gemini
                 │  parse → validate│   ├─ Ollama
                 │  → verify quote  │   └─ deterministic stub
                 │  → repair, retry │
                 └────────┬─────────┘   every attempt → llm_calls
                          │
                          ▼
        M3 actions · M4 decisions · M5 risks · M9 signals
                          │
                          ▼
                 ┌──────────────────┐
                 │  DEDUPLICATE     │  two rules: within a region,
                 └────────┬─────────┘  and across the meeting
                          │
                          ▼
        ╔═════════════════════════════════════════════════╗
        ║        M6  REVIEW QUEUE — status: pending       ║
        ║                                                 ║
        ║   THE GATE. Nothing below this line happens     ║
        ║   until a human approves.                       ║
        ║                                                 ║
        ║   edit → still pending    reject → terminal     ║
        ║   approve → writable      expire → not writable ║
        ╚════════════════════┬════════════════════════════╝
                             │  approved only
             ┌───────────────┼───────────────┬──────────────┐
             ▼               ▼               ▼              ▼
      ┌────────────┐  ┌────────────┐  ┌────────────┐ ┌────────────┐
      │ M7 TRACKER │  │ M8 INDEX   │  │ M10 DIGEST │ │ M11 OUTCOME│
      │  adapter   │  │ FTS5+FAISS │  │ scheduler  │ │  RECORD    │
      └─────┬──────┘  └─────┬──────┘  └─────┬──────┘ └─────┬──────┘
            │               │               │              │
        mock tracker    citations       notifier       document
        + write log     with spans      + markdown      store
```

---

## Where the approval gate sits

This is the question the rubric weighs most heavily, and the answer is: **in
four places, at three different depths.**

| Depth | Mechanism | Survives |
|---|---|---|
| 1 | `app/review/queue.py` refuses and explains | ordinary use |
| 2 | `app/tracker/service.py` refuses before a draft is built | the queue being bypassed |
| 3 | `trg_approval_gate_write` in `schema.sql` | the service layer being bypassed |
| 4 | `UNIQUE (tracker_writes.extraction_id)` | a forged duplicate insert |

The rubric's red flag for this criterion is gating that *"exists in the UI but
is bypassable via the API"*. So `eval/test_approval_gate.py` never goes through
HTTP. It calls the service directly, then goes lower still and opens raw SQLite
with no Python service in the path:

```python
with pytest.raises(sqlite3.IntegrityError, match="approval_gate"):
    conn.execute("INSERT INTO tracker_writes ...", (pending_id, ...))
```

**Four review states**, and only one is writable:

```
pending ──approve──► approved   ← the only writable state
        ──reject───► rejected   terminal
        ──expire───► expired    terminal, the safe default on no response
```

`approved`, `rejected` and `expired` are terminal, enforced by
`trg_extractions_terminal_status`. There is no route from `rejected` back to
`pending`, which closes the door where a rejected payload is reopened and
pushed through.

---

## Where the consent gate sits

Three layers, and the first one is why the evidence is machine-checkable.

| Layer | Where | What it does |
|---|---|---|
| 1 | `ingestion/consent.py` | Refuses on **metadata alone, before the file is opened** |
| 2 | `extraction/pipeline.py` | Refuses before any model call |
| 3 | `trg_consent_gate_insert` | An extraction cannot exist for a non-consented source |

A refused source's ingestion report carries **`bytes_read: 0`** and
`content_hash: null`. That number is the proof the content was never read,
parsed, transcribed or sent anywhere. Any one layer satisfies the requirement;
together, no code path reaches an extraction for a non-consented source.

`SourceMetadata.consent_flag` has **no default**. A source whose metadata omits
it fails validation, because absent consent is not consent.

---

## Components

| Area | Lines | Responsibility |
|---|---|---|
| `ingestion/` | 2,311 | M1 parsing and validation, M2 consent, M9 chat export |
| `extraction/` | 3,444 | The LLM wrapper, chunking, M3/M4/M5/M9 extractors |
| `retrieval/` | 1,442 | M8 embeddings, FAISS, FTS5, fusion, question answering |
| `review/` | 436 | M6 the queue and its rules |
| `tracker/` | 475 | M7 the service that owns the gate, and the write log |
| `scheduler/` | 711 | M10 APScheduler jobs and digest building |
| `outcome/` | 348 | M11 versioned records |
| `adapters/` | 820 | Three interfaces and three mocks |
| `db/` | 2,188 | `schema.sql` plus one repository per entity |
| `models/` | 1,495 | Every Pydantic contract |
| `routers/` | 1,049 | HTTP only — routing, validation, serialisation |
| `prompts/` | 356 | Five versioned prompt files |

**Layering rule, enforced by convention and by tests:** routers call services,
services call repositories, repositories are the only place SQL lives. No
business rule lives in a route handler, so the same rule holds whether it was
reached over HTTP, from the CLI, or from the eval harness.

---

## Adapters, and what they mock

The adapter contract asks one question: *could a real integration be dropped in
by writing one new class and changing one line of wiring, with zero changes to
agent logic?*

| External system | Interface | Mock | Operations |
|---|---|---|---|
| Work tracker | `adapters/tracker.py` | `MockTracker` — SQLite table + JSONL write log | `create_item`, `get_item`, `list_items`, `transition` |
| Document store | `adapters/store.py` | `MockStore` — local filesystem | `write`, `read`, `list_documents`, `exists` |
| Notification channel | `adapters/notifier.py` | `MockNotifier` — JSONL log | `post`, `list_posts` |

`adapters/factory.py` is the single line of wiring. **A test asserts that only
the factory names a concrete adapter**, so the mock's shape cannot leak into
agent logic:

```python
importers = {...}  # grep for "mock_tracker" across backend/app
assert importers == {"factory.py"}
```

That test earned its place. An earlier version had the tracker service importing
`MockTracker` to append to its write log — the mock's shape leaking into agent
logic, exactly what the contract penalises. The log moved to
`app/tracker/write_log.py` and now belongs to the agent, so a real integration
inherits it for free.

**Every operation on every interface has a caller.** `add_comment` appears in the
brief's illustrative example and is deliberately absent, because nothing in this
build comments on a ticket.

**Mocks return realistically messy data.** The tracker starts with a seeded
backlog it never created: 12 tickets, 3 with no assignee, 6 with no due date, 4
with due dates already in the past, 9 distinct free-text statuses including
`"In Progress "` with trailing whitespace beside `"In Progress"`, and a pair of
near-duplicates for the same bug raised twice. `TrackerItem` deliberately does
**not** strip whitespace, unlike every other contract here: our own contracts
normalise, foreign data is preserved as found.

---

## The LLM layer

Every model call in the system goes through one function, `call_structured`.

```
  rate limiter (token bucket, 15/min)
        ▼
  response cache  ── hit ──► return
        │ miss
        ▼
  provider.generate(prompt, json_schema)
        ▼
  parse JSON ──── fail ──► repair prompt, retry
        ▼
  validate against Pydantic ──── fail ──► repair prompt, retry
        ▼
  extra validators (quote verification) ──── fail ──► repair prompt, retry
        ▼
  return validated model        every attempt → llm_calls
```

**Failures feed the actual error back.** Not "try again": `owner: Field
required`, or the quote that failed plus how far into it the text stopped
matching.

**Quote verification is a validator inside the loop**, not a filter after it. A
fabricated quote is a validation failure like a missing field, and is repaired
by the same mechanism.

**Three providers, one interface.** Gemini and Ollama are real. `FakeProvider`
is a third real implementation, not a mock — it is how the retry loop is
testable at all, since a live model cannot be made to return broken JSON on
demand.

**Prompts are versioned files** with a declared version and a SHA-256 of the
body. Both are stored on every extraction, so a measured result can never be
attributed to a prompt that did not produce it.

---

## Storage

SQLite via raw `sqlite3`, not an ORM. `schema.sql` is then literally the schema
a reviewer reads, which is what the brief requires. **29 tables** (13 real, plus
FTS5 internals), **20 triggers**, **21 indexes**.

Business rules enforced in the database, not only in Python:

| Rule | Mechanism |
|---|---|
| No extraction for a non-consented source | `trg_consent_gate_insert` / `_update` |
| No tracker write unless approved | `trg_approval_gate_write` |
| No duplicate tracker item | `UNIQUE (tracker_writes.extraction_id)` |
| Approving requires a named reviewer and a time | `trg_extractions_review_audit` |
| Terminal states are terminal | `trg_extractions_terminal_status` |
| The model's original output is immutable | `trg_extractions_original_immutable` |
| Audit tables are append-only | 4 triggers |
| A direct message cannot be stored | `CHECK (is_direct_message = 0)` |
| `noise` is not a storable classification | `CHECK (classification IN ...)` |
| A refusal must state its reason | `trg_sources_reason_required_insert` |

The database is a **build artefact**, not source. `make seed` rebuilds it from
`schema.sql`. Seed data is disposable, so migration tooling that would never be
exercised is not carried.

---

## Retrieval

Three selectable modes, all measured on every evaluation run:

```
  question
     ├──► SQLite FTS5 (BM25, porter stemming) ──► ranked list A
     └──► FAISS IndexFlatIP over MiniLM-384d ──► ranked list B
                        │
              Reciprocal Rank Fusion (by RANK, not score)
                        │
              + neighbouring turns as separate sources
                        │
              LLM answers in claims, each with a quote
                        │
              verify each quote against THE SOURCE IT CITES
                        │
              answer with citations, or NOT FOUND
```

**Fusion is by rank, not score**, because BM25 scores and cosine similarities
are not comparable quantities and any weighted sum needs a normalisation
constant invented out of nothing — which would then be tuned on the golden set
and reported as a result.

**Verification is against the cited source**, not the corpus. A quote appearing
in source 4 while the claim cites source 2 would pass a corpus-wide check and
produce a citation nobody can follow.

**An answer whose claims all fail verification becomes a not-found**, not a
paragraph with the citations quietly removed.

Only **approved** extractions are indexed alongside transcript segments.
Answering from an unapproved one would route around the approval gate.

---

## Scheduling

APScheduler's `BackgroundScheduler`, started with the application. Two jobs:

| Job | Trigger | What it does |
|---|---|---|
| `end_of_day_digest` | cron, 18:00 | One digest per channel, approved items only, every line cited |
| `expiry_sweep` | cron, 02:00 | Pending items older than the window become `expired` |

`/api/digests/schedule` reports next fire times **from the scheduler**, not from
configuration. A time read back from settings would prove only that settings can
be read.

The expiry sweep is the rubric's *"safe default on timeout or no response"*.
Nothing is ever approved by the passage of time, and a test asserts the approved
count is unchanged after a sweep.

---

## What runs where

```
  frontend/   Vite dev server :5173   React 19 · TypeScript 6 · Tailwind 4
                    │ proxies /api and /health
                    ▼
  backend/    uvicorn :8000           FastAPI · APScheduler in-process
                    │
                    ▼
  data/       meetings.db  ·  faiss/  ·  llm_cache/  ·  documents/
  write_log/  tracker_writes.jsonl  ·  notifications.jsonl
```

All state is local. Nothing is sent anywhere except to the configured LLM
provider.

---

## Known architectural limitations

- **The scheduler runs in-process.** A second instance would run the jobs twice.
  Fine for a single-node review tool; a real deployment needs a lock or an
  external scheduler.
- **No migrations.** Changing `schema.sql` requires `make seed`, which discards
  data.
- **The vector index rebuilds in full.** Fine at 153 vectors; an incremental
  path would need deletion handling nothing here exercises.
- **Reviewer identity is a supplied name**, not an authenticated user.
  Authentication is explicitly out of scope in the brief.
- **Calling an adapter directly bypasses the audit row.** Documented in
  `decision_log.md` L13 as a boundary rather than claimed impossible: the item
  exists but has no `tracker_writes` row, so the accounting shows it for what it
  is.

Full reasoning for every choice above is in [`decision_log.md`](../decision_log.md),
91 decisions and 27 recorded limitations. Per-component detail is in
[`components/`](components/).
