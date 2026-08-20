# CLAUDE.md — Meeting & Channel Intelligence Agent

## What this is

A 7-day intern selection challenge from DigitalT3. Build an agent that ingests meeting transcripts and chat exports, extracts action items, decisions and risks with verbatim quotes, puts everything through a human approval queue, writes approved items to a mock tracker, answers questions with citations, classifies chat signals, and produces scheduled digests.

The system must be honest, traceable, human-gated, and measured. No SaaS integrations needed. All mocks.

## The one rule that matters

Nothing gets written to any external system without explicit human approval. This is tested and is an automatic failure if violated.

---

## Tech stack (chosen, justified)

| Layer | Choice | Why |
|---|---|---|
| Language | Python 3.11+ | Fastest for Esandu. FastAPI experience. Pydantic for structured output. |
| API | FastAPI | Thin routing, Pydantic validation built in. Already proven in Lune AI. |
| LLM | Google Gemini free tier (gemini-2.0-flash) via google-genai SDK. Fallback: Ollama with llama3.1:8b or qwen2.5:7b locally. | Free tier, good at structured JSON output, fast. Wrap in a single function so provider is swappable. |
| Structured output | Pydantic models + JSON schema constraint + retry-on-parse-failure loop | Mandatory requirement. Never parse with regex or string split. |
| Data store | SQLite via SQLAlchemy (or raw sqlite3) | Zero setup, schema visible in repo as migration/model file. |
| Search / retrieval | SQLite FTS5 (full-text search) | Keyword search that works and is measured beats a vector store that isn't evaluated. Simpler, no embedding model needed. If time permits, add FAISS with all-MiniLM-L6-v2 as an upgrade. |
| Speech to text | faster-whisper (tiny or base model) | Only if audio files provided. Pre-supplied transcripts are the primary path. Transcription quality is NOT scored. |
| Scheduling | APScheduler (BackgroundScheduler) | Real scheduler, clock-override for demo. |
| UI | Streamlit | Fastest path to a usable approval queue. Visual polish is NOT scored. The review surface is what matters. |
| Testing | pytest | Meaningful tests on parsing and business logic. |
| Eval harness | Plain Python script over golden dataset, prints table, commits results | This is scored heavily. Worth more than the UI. |
| Packaging | Docker + docker-compose, plus a Makefile with `make setup`, `make seed`, `make run` | Must work from clean clone in one command. |
| Version control | Git, public repo, meaningful incremental commits across 7 days | One giant commit = red flag. |

---

## Project structure

```
meeting-intelligence-agent/
├── CLAUDE.md                    # This file
├── README.md                    # Status table, architecture, setup
├── Makefile                     # make setup, make seed, make run, make eval
├── docker-compose.yml
├── Dockerfile
├── .env.example                 # Model provider, API keys if any
├── requirements.txt
├── decision_log.md              # Scope cuts, assumptions, known limitations
│
├── src/
│   ├── __init__.py
│   ├── main.py                  # FastAPI app entry point
│   ├── config.py                # Pydantic Settings, env vars
│   │
│   ├── models/                  # Pydantic models = the data contracts
│   │   ├── __init__.py
│   │   ├── source.py            # Source, Segment (transcript record)
│   │   ├── extraction.py        # ActionItem, Decision, Risk, ChatSignal
│   │   ├── review.py            # ReviewableItem (status: pending/approved/rejected)
│   │   ├── tracker.py           # TrackerItem, WriteLog
│   │   ├── outcome.py           # OutcomeRecord (versioned, M11)
│   │   └── digest.py            # DigestEntry
│   │
│   ├── db/
│   │   ├── __init__.py
│   │   ├── schema.sql           # SQLite schema, committed and visible
│   │   ├── database.py          # Connection, init, migrations
│   │   └── queries.py           # All DB operations
│   │
│   ├── ingestion/
│   │   ├── __init__.py
│   │   ├── transcript.py        # M1: Parse txt/vtt/json transcripts, normalise
│   │   ├── audio.py             # M1: faster-whisper for audio files (optional)
│   │   ├── chat_export.py       # M9: Parse chat export, exclude DMs
│   │   ├── consent.py           # M2: Consent gate, hard block
│   │   └── validator.py         # Malformed file detection, encoding checks
│   │
│   ├── extraction/
│   │   ├── __init__.py
│   │   ├── llm_client.py        # Single wrapper function, provider-swappable
│   │   ├── chunker.py           # Chunk with overlap, preserve timestamps/speakers
│   │   ├── actions.py           # M3: Extract action items
│   │   ├── decisions.py         # M4: Extract decisions (distinguish decided vs deferred)
│   │   ├── risks.py             # M5: Extract risks and blockers
│   │   ├── signals.py           # M9: Classify chat signals
│   │   ├── quote_verifier.py    # Substring check on every quote, retry on failure
│   │   └── deduplicator.py      # Cross-chunk deduplication
│   │
│   ├── prompts/                 # One file per capability, loaded at runtime
│   │   ├── extract_actions.txt
│   │   ├── extract_decisions.txt
│   │   ├── extract_risks.txt
│   │   ├── classify_signals.txt
│   │   ├── answer_question.txt
│   │   └── draft_followup.txt
│   │
│   ├── review/
│   │   ├── __init__.py
│   │   └── queue.py             # M6: Review queue logic, approve/reject/edit
│   │
│   ├── adapters/
│   │   ├── __init__.py
│   │   ├── tracker_interface.py # Abstract interface: list_items, get_item, create_item, transition
│   │   ├── mock_tracker.py      # Mock: local SQLite table + inspectable write log (JSONL)
│   │   ├── store_interface.py   # Abstract interface for document/file store
│   │   ├── mock_store.py        # Mock: local file system
│   │   └── factory.py           # One factory function, reads config, returns implementation
│   │
│   ├── retrieval/
│   │   ├── __init__.py
│   │   └── qa.py                # M8: Question answering with citations, not-found path
│   │
│   ├── scheduler/
│   │   ├── __init__.py
│   │   └── digest.py            # M10: APScheduler end-of-day digest, clock override
│   │
│   ├── outcome/
│   │   ├── __init__.py
│   │   └── record.py            # M11: Versioned structured outcome record
│   │
│   └── routers/
│       ├── __init__.py
│       ├── ingest.py            # POST /ingest — upload transcript or chat
│       ├── extract.py           # POST /extract/{source_id} — trigger extraction
│       ├── review.py            # GET/POST /review — list pending, approve/reject
│       ├── tracker.py           # GET /tracker — inspect mock writes
│       ├── qa.py                # POST /qa — question answering
│       └── digest.py            # GET /digest, POST /digest/trigger
│
├── ui/
│   └── app.py                   # Streamlit app: review queue, Q&A, digest view
│
├── sample_data/
│   ├── transcripts/
│   │   ├── sprint_planning.txt       # Valid, multi-speaker, planted difficulties
│   │   ├── client_status_call.txt    # Valid, multi-speaker, planted difficulties
│   │   ├── no_consent_meeting.txt    # consent=false
│   │   └── malformed_meeting.txt     # Truncated, missing speakers, bad encoding
│   ├── chat_export/
│   │   └── channels.json             # 60-120 messages, 2 project channels + 1 DM thread
│   ├── metadata/
│   │   └── sources.json              # Title, date, participants, consent flag per source
│   └── golden/
│       ├── golden_actions.json       # Hand-labelled action items
│       ├── golden_decisions.json     # Hand-labelled decisions + deferred item
│       ├── golden_risks.json         # Hand-labelled risks
│       ├── golden_questions.json     # 5 answerable + 1 unanswerable
│       └── golden_signals.json       # 20 hand-labelled chat messages
│
├── eval/
│   ├── harness.py               # Runs all golden test cases, prints table
│   ├── results.txt              # Committed output from final run
│   └── test_approval_gate.py    # Golden case 8: API-level enforcement test
│
├── tests/
│   ├── test_consent_gate.py
│   ├── test_quote_verifier.py
│   ├── test_chunker.py
│   ├── test_review_queue.py
│   ├── test_tracker_idempotency.py
│   └── test_deduplication.py
│
├── docs/
│   ├── architecture.md          # One page: components, data flow, approval gate placement
│   └── outcome_schema.json      # Versioned schema for M11
│
└── write_log/
    └── tracker_writes.jsonl     # Inspectable log of every mock tracker write
```

---

## Database schema (SQLite)

```sql
-- Sources: ingested transcripts and chat exports
CREATE TABLE sources (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    source_type TEXT NOT NULL CHECK(source_type IN ('transcript', 'chat_export')),
    date TEXT,
    participants TEXT,  -- JSON array
    consent_flag BOOLEAN NOT NULL,
    file_path TEXT,
    ingested_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('ingested', 'refused', 'error'))
);

-- Segments: normalised transcript lines
CREATE TABLE segments (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources(id),
    speaker TEXT,
    start_timestamp TEXT,
    end_timestamp TEXT,
    text TEXT NOT NULL,
    segment_index INTEGER NOT NULL
);

-- FTS5 virtual table for full-text search across segments
CREATE VIRTUAL TABLE segments_fts USING fts5(
    text,
    content='segments',
    content_rowid='rowid'
);

-- Extractions: actions, decisions, risks, chat signals
-- All go through review queue. Status enforces approval gate.
CREATE TABLE extractions (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources(id),
    extraction_type TEXT NOT NULL CHECK(extraction_type IN ('action', 'decision', 'risk', 'signal')),
    payload TEXT NOT NULL,       -- JSON: the structured extraction
    original_payload TEXT,       -- JSON: model's original output, retained after edits
    verbatim_quote TEXT NOT NULL,
    quote_verified BOOLEAN NOT NULL DEFAULT 0,
    speaker TEXT,
    timestamp TEXT,
    confidence REAL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'approved', 'rejected')),
    reviewer TEXT,
    reviewed_at TEXT,
    created_at TEXT NOT NULL
);

-- Tracker writes: only from approved extractions
CREATE TABLE tracker_writes (
    id TEXT PRIMARY KEY,
    extraction_id TEXT NOT NULL REFERENCES extractions(id),
    write_payload TEXT NOT NULL,  -- JSON
    written_at TEXT NOT NULL,
    deduplicated BOOLEAN NOT NULL DEFAULT 0
);

-- Chat messages (from chat export ingestion)
CREATE TABLE chat_messages (
    id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources(id),
    channel TEXT NOT NULL,
    author TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    thread_id TEXT,
    text TEXT NOT NULL,
    is_direct_message BOOLEAN NOT NULL DEFAULT 0,
    classification TEXT,  -- decision/blocker/question/request/noise/null
    classification_confidence REAL
);

-- Digests
CREATE TABLE digests (
    id TEXT PRIMARY KEY,
    channel TEXT,
    generated_at TEXT NOT NULL,
    content TEXT NOT NULL,  -- JSON: items moved, items needing attention, things to decide
    contains_only_approved BOOLEAN NOT NULL DEFAULT 1
);
```

---

## Capability build order (matches the 7-day plan)

### Day 1: Setup, ingest, consent gate

**M1 — Ingest and normalise a source**
- Parse .txt, .vtt, .json transcript files into ordered segments with speaker label, start timestamp, text
- Generate a stable source_id (hash of content + metadata)
- Store in SQLite sources + segments tables
- Handle malformed files gracefully: detect truncation, missing speakers, invalid encoding. Reject with clear reason, never corrupt the store.
- For audio: use faster-whisper tiny model, output segments. This is optional, text transcripts are the primary path.

**M2 — Consent gate**
- Check source metadata consent_flag BEFORE any extraction or model call
- If consent=false: store a refusal record (source with status='refused'), process nothing, send nothing to any model
- This is a hard block in code, not a prompt instruction
- Test: consent=false source produces zero extracted items, zero model calls

### Day 2: Action extraction, end-to-end slice (CRITICAL DAY)

**M3 — Extract action items**
- Chunk transcript with overlap (chunk size ~2000 tokens, overlap ~200 tokens). Preserve speaker labels and timestamps across chunk boundaries.
- For each chunk, call LLM with structured output schema:
  ```python
  class ActionItem(BaseModel):
      what: str
      owner: str  # "UNSPECIFIED" if not stated
      due_date: str  # "UNSPECIFIED" if not stated, or resolved relative date with rule documented
      verbatim_quote: str
      speaker: str
      timestamp: str
      confidence: float
  ```
- JSON schema constrained output. Retry on parse failure (up to 3 attempts).
- Quote verification: assert verbatim_quote is a literal substring of the source transcript. If not, retry the model with the failure fed back. If still fails after retries, flag the item but keep it with quote_verified=false.
- Deduplicate across chunks (same action extracted from overlapping regions).
- Every action lands in extractions table with status='pending'.
- Owner: if no owner stated in transcript, output "UNSPECIFIED". NEVER guess.
- Due date: if no date stated, output "UNSPECIFIED". If relative date ("by end of next week"), resolve it and document the resolution rule. NEVER invent a date.

**M6 (start) — Review queue, crude first version**
- GET /review: list all pending extractions
- POST /review/{id}/approve: set status='approved', record reviewer + timestamp, retain original_payload
- POST /review/{id}/reject: set status='rejected', record reviewer + timestamp
- POST /review/{id}/edit: update payload, retain original in original_payload, keep status as pending (user then approves)

**First 5 golden test cases in the eval harness**

### Day 3: Approval enforcement, tracker adapter, writes

**M6 (complete) — Review and approval queue**
- Approval enforced in the data model, not just the UI
- API-level test: attempting a tracker write for a pending or rejected record MUST fail
- The Streamlit UI shows the queue but the gate is in the service layer

**M7 — Write approved items to tracker**
- Define tracker adapter interface (abstract class):
  ```python
  class TrackerInterface(ABC):
      @abstractmethod
      def list_items(self, filter: dict) -> list: ...
      @abstractmethod
      def get_item(self, id: str) -> dict: ...
      @abstractmethod
      def create_item(self, payload: dict) -> dict: ...
      @abstractmethod
      def transition(self, id: str, status: str) -> dict: ...
  ```
- MockTracker: backed by SQLite tracker_writes table + JSONL write log
- Idempotency: re-approval must NOT create duplicates. Check extraction_id before writing. Log deduplicated attempts.
- Agent logic imports the interface, never the mock directly
- Factory function reads config to choose implementation

### Day 4: Decisions, risks, question answering

**M4 — Extract decisions**
- Same extraction + validation pattern as M3
- Must distinguish "we decided X" from "we discussed X and deferred it"
- Prompt must explicitly instruct: if a decision was proposed then deferred, do NOT record it as a decision
- Golden case 5 tests this specifically
- Schema:
  ```python
  class Decision(BaseModel):
      what_was_decided: str
      stated_rationale: str
      who_stated_it: str
      verbatim_quote: str
      timestamp: str
      alternatives_discussed: list[str]
  ```

**M5 — Extract risks and blockers**
- Schema:
  ```python
  class Risk(BaseModel):
      description: str
      severity: str  # "high", "medium", "low"
      affected_area: str
      owner: str  # "UNSPECIFIED" if none named
      verbatim_quote: str
      timestamp: str
  ```
- Severity must be defensible from the quote alone

**M8 — Cross-source question answering**
- SQLite FTS5 search across segments table
- Also search across approved extractions
- Answer with citations: source_id, timestamp, quoted span
- When nothing supports an answer: return explicit "not found in the available sources" (not a plausible fabrication)
- Golden case 6: 5 questions with correct source in top 3, 1 unanswerable returning not-found

### Day 5: Chat signals, scheduler, outcome records

**M9 — Ingest and classify chat signals**
- Parse chat export JSON (messages with channel, author, timestamp, thread_id)
- Exclude direct messages by construction (is_direct_message=true never processed)
- Classify each relevant message: decision / blocker / question / request / noise
- Noise is discarded, not stored
- Anything that would produce a downstream write goes through the same approval queue

**M10 — Scheduled end-of-day digest**
- APScheduler BackgroundScheduler, configurable time
- Clock override for demo (pass a fake "now" to produce digest for any date)
- Digest per channel: "3 items that moved, 2 items that need attention, 1 thing to decide"
- Every line cites its source
- Digests NEVER contain unapproved extractions
- The scheduler must be real, visible in code, not a button pretending to be a scheduler

**M11 — Structured outcome record**
- One versioned JSON record per source, written after approval
- Schema documented in docs/outcome_schema.json with a version field
- Contains approved items only, carries consent flag forward
- A second process can reconstruct approved items without access to transcript store

### Day 6: Harden, measure, document

- Run full eval harness, commit results
- Fix the worst failure the harness reveals
- Test edge cases: malformed source, empty transcript, transcript with zero commitments
- Graceful handling: model rate limits, parse failures, missing fields
- Write README status table from actual code state
- Architecture note/diagram
- Decision log

### Day 7: Demo

- Verify from clean clone in a different directory
- Record 5-10 minute walkthrough showing:
  - Ingest a transcript
  - Consent refusal for the blocked source
  - Extraction with verbatim quotes
  - An UNSPECIFIED owner handled honestly
  - Approve an item -> mock tracker write
  - Reject an item -> no write
  - A cited answer from Q&A
  - A not-found answer
  - The digest
  - Eval harness output
- Close with honest assessment of weakest part
- DO NOT add features on Day 7

---

## Critical implementation details

### Chunking strategy

The brief says: "Chunking will decide your extraction quality."

```python
def chunk_transcript(segments: list[Segment], max_tokens: int = 2000, overlap_tokens: int = 200) -> list[Chunk]:
    """
    Chunk by segment boundaries, not raw character count.
    Each chunk:
    - Contains complete segments (never split mid-sentence)
    - Carries the speaker label and timestamp of every segment it contains
    - Overlaps with the previous chunk by ~overlap_tokens worth of segments
    - Has a chunk_id and references back to segment IDs
    
    This matters because:
    - A commitment might span two segments from the same speaker
    - Timestamps must survive chunking for citation
    - Overlap prevents missing items at chunk boundaries
    - Deduplication across chunks catches items extracted from overlapping regions
    """
```

### Quote verification (the most important 5 lines)

```python
def verify_quote(quote: str, transcript_text: str) -> bool:
    """
    Assert that the verbatim_quote is a literal substring of the source transcript.
    Normalise whitespace before comparing.
    This is the single most important quality check in the system.
    """
    normalised_quote = " ".join(quote.split())
    normalised_source = " ".join(transcript_text.split())
    return normalised_quote in normalised_source
```

If verification fails, retry the model with feedback:
```
The quote you provided is not a literal substring of the transcript.
The quote was: "{failed_quote}"
Please re-extract, using only exact text from the transcript as the verbatim_quote.
```

### LLM wrapper (single function, provider-swappable)

```python
# src/extraction/llm_client.py

import json
from pydantic import BaseModel
from src.config import settings

def call_llm(
    prompt: str,
    response_model: type[BaseModel],
    max_retries: int = 3
) -> BaseModel:
    """
    Single wrapper. Provider is swapped by changing config.
    Returns a validated Pydantic model or raises after max_retries.
    
    Flow:
    1. Call model with JSON schema instruction
    2. Parse response as JSON
    3. Validate against Pydantic model
    4. If parse or validation fails, retry with error feedback
    5. If all retries fail, raise with details
    """
```

### Approval gate enforcement (in code, not just UI)

```python
# src/adapters/mock_tracker.py

def create_item(self, extraction_id: str) -> dict:
    extraction = db.get_extraction(extraction_id)
    
    if extraction.status != "approved":
        raise PermissionError(
            f"Cannot write extraction {extraction_id}: status is '{extraction.status}', "
            f"only 'approved' extractions can be written to tracker"
        )
    
    # Idempotency check
    existing = db.get_tracker_write_by_extraction(extraction_id)
    if existing:
        db.log_write_attempt(extraction_id, deduplicated=True)
        return existing  # No duplicate created
    
    # Write and log
    item = {...}
    db.insert_tracker_write(item)
    db.log_write_attempt(extraction_id, deduplicated=False)
    return item
```

### Adapter pattern (the test they apply)

"Could a real integration be dropped in by writing one new class and changing one line of wiring, with zero changes to agent logic?"

```python
# src/adapters/factory.py
from src.config import settings
from src.adapters.tracker_interface import TrackerInterface
from src.adapters.mock_tracker import MockTracker

def get_tracker() -> TrackerInterface:
    if settings.TRACKER_PROVIDER == "mock":
        return MockTracker(db_path=settings.DB_PATH)
    # Future: elif settings.TRACKER_PROVIDER == "jira": return JiraTracker(...)
    raise ValueError(f"Unknown tracker provider: {settings.TRACKER_PROVIDER}")
```

Agent code:
```python
from src.adapters.factory import get_tracker

tracker = get_tracker()  # Never imports MockTracker directly
tracker.create_item(payload)
```

### Prompts: one file per capability

Store in `src/prompts/`, load at runtime:
```python
def load_prompt(name: str) -> str:
    path = Path(__file__).parent / "prompts" / f"{name}.txt"
    return path.read_text()
```

This means prompts can be versioned, diffed, and the eval harness tests a specific prompt version.

---

## Evaluation harness (scored heavily, worth more than the UI)

```python
# eval/harness.py

"""
Runs all 8 golden test cases.
Prints one line per metric with measured value and target.
Writes results to eval/results.txt.

Metrics:
1. Action recall:           target >= 0.7
2. Fabricated quote count:  target = 0 (MOST IMPORTANT)
3. Owner accuracy:          target >= 0.9 (where owner IS named)
4. UNSPECIFIED compliance:  actions with no owner return UNSPECIFIED
5. Invented date count:     target = 0
6. Deferred decision test:  deferred item NOT in decision log
7. Retrieval accuracy:      correct source in top 3 for 5 questions, not-found for 1
8. Chat signal precision:   target >= 0.7, zero DM records
9. Approval gate test:      pending/rejected writes fail at API level

Output format:
┌──────────────────────────┬──────────┬────────┬────────┐
│ Metric                   │ Measured │ Target │ Status │
├──────────────────────────┼──────────┼────────┼────────┤
│ Action recall            │ 0.80     │ 0.70   │ PASS   │
│ Fabricated quotes        │ 0        │ 0      │ PASS   │
│ Owner accuracy           │ 0.92     │ 0.90   │ PASS   │
│ UNSPECIFIED compliance   │ 2/2      │ 2/2    │ PASS   │
│ ...                      │          │        │        │
└──────────────────────────┴──────────┴────────┴────────┘
"""
```

---

## Sample data to create

Generate these with an LLM (state so in README, that's expected and fine).

### Transcript 1: Sprint Planning (valid, ~15 min)
- 4-5 speakers, one who never speaks
- Contains: 5-6 clear action items, 2 decisions, 1 risk
- Plant: 2 commitments with NO owner ("someone needs to check the migration path")
- Plant: 1 commitment with a relative date ("by end of next week")
- Plant: 1 action stated by person on behalf of someone else
- Plant: 2 people with similar first names (e.g. "Priya S." and "Priya M.")
- Plant: 1 long digression with zero commitments

### Transcript 2: Client Status Call (valid, ~20 min)
- 3-4 speakers
- Contains: 4-5 action items, 1 decision, 1 risk
- Plant: 1 decision that is proposed and then explicitly deferred (CRITICAL for golden case 5)
- Plant: 1 commitment with no due date

### Transcript 3: No Consent Meeting
- consent_flag = false in metadata
- Content doesn't matter, it should never be processed

### Transcript 4: Malformed Meeting
- Truncated mid-sentence
- Missing speaker labels on some segments
- Could include invalid encoding bytes
- System must reject with clear reason, not crash

### Chat Export: 80-100 messages
- Channel A (project): mix of decisions, blockers, questions, requests, noise
- Channel B (project): similar
- Channel C (DM thread): must be excluded entirely from processing
- 20 messages hand-labelled for golden case 7

---

## README status table template

```markdown
| ID  | Capability                        | Priority | Status    | Notes |
|-----|-----------------------------------|----------|-----------|-------|
| M1  | Ingest and normalise a source     | MUST     | Done      |       |
| M2  | Consent gate                      | MUST     | Done      |       |
| M3  | Extract action items              | MUST     | Done      |       |
| M4  | Extract decisions                 | MUST     | Done      |       |
| M5  | Extract risks and blockers        | SHOULD   | Done      |       |
| M6  | Review and approval queue         | MUST     | Done      |       |
| M7  | Write approved items to tracker   | MUST     | Done      |       |
| M8  | Cross-source question answering   | MUST     | Done      |       |
| M9  | Chat signal classification        | SHOULD   | Partial   |       |
| M10 | Scheduled end-of-day digest       | SHOULD   | Done      |       |
| M11 | Structured outcome record         | SHOULD   | Not built |       |
| M12 | Follow-up message draft           | COULD    | Not built |       |
| M13 | Per-person digest                 | COULD    | Not built |       |
```

Fill this from the code on Day 6, not from intentions.

---

## Anti-patterns to avoid (from the brief, these have sunk previous candidates)

1. README describes features the code doesn't contain — automatic failure
2. Empty placeholder files for unbuilt integrations — delete them
3. No human approval gate — automatic failure
4. No evaluation harness — "most candidates skip this; do not be most candidates"
5. Silent guessing on ambiguity (inventing owners/dates) — most damaging failure mode
6. Prompts scattered inline through code — one file per capability
7. All work landing on Day 6-7 — commit incrementally
8. Building a generic tool instead of the specified agent
9. Fabricated evaluation results — automatic failure
10. Secrets committed to repo — automatic failure

---

## Commit strategy (they read git history)

- Day 1: "feat: project setup, SQLite schema, M1 ingestion, M2 consent gate"
- Day 2: "feat: M3 action extraction with quote verification and retry loop"
- Day 2: "feat: M6 review queue (basic), first eval harness cases"
- Day 3: "feat: tracker adapter interface + mock, M7 writes with idempotency"
- Day 3: "test: approval gate enforcement at API level"
- Day 4: "feat: M4 decisions, M5 risks extraction"
- Day 4: "feat: M8 question answering with FTS5 and citations"
- Day 5: "feat: M9 chat ingestion and signal classification"
- Day 5: "feat: M10 scheduled digest with APScheduler"
- Day 6: "fix: [whatever the eval harness reveals]"
- Day 6: "docs: README status table, architecture, decision log"
- Day 7: "docs: final eval results committed"

---

## Key decisions to document in decision_log.md

1. SQLite over PostgreSQL: zero setup, the brief suggests it as the default, schema is visible
2. Gemini free tier over local model: faster iteration, good structured output support. Wrapped in swappable function.
3. FTS5 over vector search: keyword search that works and is measured beats vector store without evaluation. The brief explicitly says this.
4. Streamlit over React: 7-day constraint, review queue functionality over visual polish
5. APScheduler over cron: runs inside the Python process, clock-override is trivial for demo
6. Chunk by segment boundaries, not raw characters: preserves speaker labels and timestamps

---

## What to say in the README about AI assistant usage

"Built with Claude Code as the primary AI coding assistant, used for scaffolding, implementation, and debugging. Every design decision was made by the candidate and can be explained and defended in the walkthrough."
