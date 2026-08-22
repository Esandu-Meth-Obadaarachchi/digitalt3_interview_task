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
rolled into scheduled digests for each channel and for each person, drafted into
a recap for somebody to edit and send, and emitted as versioned outcome records
that a downstream agent can consume without this application. The agent sends
nothing itself.

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
      ┌──────┬─────────┼───────────────┬──────────────┬─────────────┐
      ▼      ▼         ▼               ▼              ▼             ▼
 ┌─────────┐ ┌──────────┐  ┌────────────┐ ┌──────────┐ ┌──────────────┐
 │M7TRACKER│ │M8 INDEX  │  │ M10 DIGEST │ │M11OUTCOME│ │ M12 FOLLOW-UP│
 │ adapter │ │FTS5+FAISS│  │ M13 PERSON │ │  RECORD  │ │    DRAFT     │
 └────┬────┘ └────┬─────┘  └─────┬──────┘ └────┬─────┘ └──────┬───────┘
      │           │              │             │              │
  mock tracker  citations     notifier      document      a person edits
  + write log   with spans    + markdown     store        ▼ and sends it
                                                    ┌──────────────┐
                                                    │ SEND GATE    │
                                                    │ named person │
                                                    │ or refused   │
                                                    └──────────────┘
```

The send gate is the only one below the review queue. Everything else under
that line is a write the approval already authorised. A recap addressed to
people is not: M12 says the agent never sends, so a second, differently shaped
gate stands in front of it and asks for a name.

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

## Where the send gate sits

The second gate, and the only one below the review queue. M12 says *"Human
edits and sends. Agent never sends."* Drafting a recap is not an external write:
nothing leaves the machine, and every line already passed the approval gate.
Sending is, so `sent_by` is refused four ways.

| Depth | Mechanism | Refuses |
|---|---|---|
| 1 | `followup/draft.py` | a blank name → `AgentSendRefused`, 403 |
| 2 | `followup/draft.py` | `agent`, `system`, `scheduler`, `bot`, `service`, `llm`, `model` |
| 3 | `trg_followup_send_requires_person` | any UPDATE to `sent` with no `sent_by` |
| 4 | `trg_followup_agent_cannot_send` | any UPDATE to `sent` naming a service |

`trg_followup_insert_is_draft` closes the way round: a row arriving already
marked sent would walk past every rule written on UPDATE, so a draft is born a
draft. `trg_followup_sent_is_final` stops a sent message being rewritten
afterwards to say something the sender did not send.

The HTTP endpoint has **no default for `sent_by`**, and a test asserts a request
omitting it fails validation. A default would be the agent sending under
whatever name the default carried.

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
| `extraction/` | 2,351 | The LLM wrapper, chunking, M3/M4/M5/M9 extractors |
| `db/` | 1,846 | `schema.sql` plus one repository per entity |
| `ingestion/` | 1,644 | M1 parsing and validation, M2 consent, M9 chat export |
| `models/` | 1,263 | Every Pydantic contract |
| `retrieval/` | 991 | M8 embeddings, FAISS, FTS5, fusion, question answering |
| `routers/` | 831 | HTTP only — routing, validation, serialisation |
| `scheduler/` | 710 | M10 APScheduler jobs, M10 channel and M13 person digests |
| `adapters/` | 526 | Three interfaces and three mocks |
| `prompts/` | 356 | Five versioned prompt files |
| `followup/` | 329 | M12 the recap draft, and the send refusal |
| `tracker/` | 296 | M7 the service that owns the gate, and the write log |
| `review/` | 276 | M6 the queue and its rules |
| `outcome/` | 208 | M11 versioned records |
| `people/` | 166 | M13 deciding which commitments belong to the same person |

12,244 lines across `backend/app`, counted with

```bash
find backend/app -type f \( -name '*.py' -o -name '*.sql' -o -name '*.txt' \) \
  -not -path '*__pycache__*' -print0 | xargs -0 cat | wc -l
```

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
a reviewer reads, which is what the brief requires. The built database holds
**30 tables** — 14 application tables, `schema_meta`, 3 FTS5 virtual tables and
their 12 shadow tables — with **25 triggers** and **23 indexes**.

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
| A recap cannot be sent with nobody behind it | `trg_followup_send_requires_person` |
| A service cannot send a recap | `trg_followup_agent_cannot_send` |
| A draft is born a draft | `trg_followup_insert_is_draft` |
| A sent message cannot be rewritten | `trg_followup_sent_is_final` |
| The generated recap text is immutable | `trg_followup_generated_body_immutable` |

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
| `end_of_day_digest` | cron, 18:00 | One digest per channel (M10), then one per person who has approved commitments (M13). Approved items only, every line cited |
| `expiry_sweep` | cron, 02:00 | Pending items older than the window become `expired` |

`/api/digests/schedule` reports next fire times **from the scheduler**, not from
configuration. A time read back from settings would prove only that settings can
be read.

The expiry sweep is the rubric's *"safe default on timeout or no response"*.
Nothing is ever approved by the passage of time, and a test asserts the approved
count is unchanged after a sweep.

**Nothing scheduled sends anything.** Channel digests are posted through the
notifier, which the task catalogue is explicit is not an external write, since
every line already passed the approval gate. The M12 recap is different: it is
addressed to people, and M12 says the agent never sends. A test asserts that no
file under `app/scheduler` so much as mentions follow-ups, because a scheduled
job that sent a recap would pass every behavioural test and break the one rule
the capability states.

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
- **People are grouped by first name.** Two participants sharing one share a
  digest. Deliberate, and the digest says which full names it covers, but it is
  a heuristic standing in for an identity service this build does not have.

Full reasoning for every choice above is in [`decision_log.md`](../decision_log.md).
Per-component detail is in [`components/`](components/).
