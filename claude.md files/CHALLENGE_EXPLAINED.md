# What you're building, explained simply

## The problem this agent solves

Teams have meetings. Decisions get made. Action items get assigned. Then everyone walks out and forgets half of it. Notes are inconsistent. Nobody can find "when did we agree to defer the reporting module?" three weeks later.

Worse: existing AI note-takers write fluent summaries that sound right but can't be verified. That's more dangerous than no notes at all, because people trust them.

Your agent fixes this by being traceable and honest. Every fact it extracts links back to a verbatim quote from the transcript. If it can't find who owns a task, it says "UNSPECIFIED" instead of guessing. Nothing gets written anywhere until a human approves it.

## The 13 capabilities, in plain terms

There are 7 MUST (you fail without these), 4 SHOULD (separates good from strong), and 2 COULD (stretch, only after everything else works).

### MUST (build all of these)

**M1 — Ingest a transcript.** Take a meeting transcript file (text, VTT subtitles, or JSON), parse it into ordered lines with speaker names and timestamps, store it. Also handle audio files if you want (not required, use the text transcripts). When a file is broken (truncated, bad encoding), reject it cleanly, don't crash.

**M2 — Consent gate.** Before doing ANYTHING with a meeting, check if consent was given. If the metadata says consent=false, hard stop. Don't transcribe it, don't send it to any AI model, don't extract anything. Store a refusal record and move on. This is a code check, not a prompt instruction.

**M3 — Extract action items.** Feed the transcript to an LLM chunk by chunk. Get back structured data: what's the task, who owns it, when is it due, and what's the exact quote from the meeting. The quote MUST be a literal copy-paste from the transcript (substring check). If the model makes up a quote, retry. If nobody was named as owner, output "UNSPECIFIED", never guess.

**M4 — Extract decisions.** Same pattern. The hard part: distinguish "we decided X" from "we discussed X and then deferred it." The deferred one must NOT end up in your decisions log. There's a golden test case specifically for this.

**M5 — Extract risks and blockers.** (This is listed as SHOULD in the task catalog but you should build it.) Same extraction pattern, with a severity label that's defensible from the quote alone.

**M6 — Review and approval queue.** Every extraction (action, decision, risk, chat signal) lands in a queue as "pending." A human reviews it, can edit the payload, then approves or rejects. The original model output is kept alongside any edits. The approval gate must be enforced in your data model and API, not just in the UI. If someone calls your API directly to write a pending item to the tracker, it must fail.

**M7 — Write to tracker (mock).** When an action is approved, write it to a mock issue tracker. The mock is backed by a local table and a JSONL write log you can inspect. Idempotency: if the same item gets re-approved, it must NOT create a duplicate. The write log shows every attempt, including deduplicated ones.

**M8 — Question answering.** A user asks "when did we agree to defer the reporting module?" and your system searches across all stored transcripts and extractions, returns an answer with citations (source ID, timestamp, quoted text). When nothing in the data answers the question, return "not found in the available sources" instead of making something up.

### SHOULD (build these after MUST is solid)

**M9 — Chat signal classification.** Ingest a chat export (like a Slack export). Classify each message: is it a decision, a blocker, a question, a request, or noise? Noise gets discarded. Direct messages are excluded entirely, never processed.

**M10 — Scheduled digest.** A real scheduler (APScheduler, not a button) runs at end of day. Produces a digest per channel: 3 items that moved, 2 that need attention, 1 thing to decide. Every line cites its source. Never includes unapproved extractions. Must have a clock override so you can demo it.

**M11 — Structured outcome record.** A versioned JSON file per meeting containing all the approved extractions. Schema documented, another system can read it without needing the raw transcript.

### COULD (stretch, only if everything above is done)

**M12 — Follow-up message draft.** Generate a recap email/message from approved items. Human edits and sends. Agent never sends.

**M13 — Per-person digest.** Per-person view of their commitments. Person with no commitments gets no digest.

## What they're actually scoring

This is not a "build as much as possible" exercise. They're testing four things:

1. **Does your system tell the truth and prove it?** (verbatim quotes, citations, UNSPECIFIED instead of guessing)
2. **Does a human approve before anything irreversible?** (approval gate, enforced in code)
3. **Do you measure your own output quality?** (evaluation harness with golden test cases)
4. **Do you make explicit scope decisions?** (what's built, what's cut, documented honestly)

The scoring rubric weights:
- Functional coverage (MUST capabilities): 28%
- Grounding and citation discipline: 12%
- Human-in-the-loop gating: 10%
- Evaluation harness and measured quality: 12%
- Architecture and swappability: 10%
- Robustness and failure handling: 8%
- Repo hygiene and honest documentation: 8%
- Demo quality and ability to defend: 8%
- Judgement and initiative: 4%

Notice: the evaluation harness (12%) is worth more than the UI. The approval gate (10%) is worth more than repo hygiene. Quote integrity is literally the most important number in the submission.

## Automatic failures (any of these = instant rejection)

1. README says features exist that the code doesn't have
2. Agent writes to external system with no approval path
3. API keys committed to git
4. Can't explain your own code in Q&A
5. Eval results in README that the harness can't reproduce

## What you're building, the flow in order

```
Transcript file → Parse into segments → Consent check →
  → Chunk with overlap → LLM extraction (per chunk) →
  → Quote verification (substring check) → Deduplication →
  → Into review queue (pending) →
  → Human approves/rejects →
  → If approved → Mock tracker write (idempotent) + write log

Chat export → Parse → Exclude DMs → Classify signals →
  → Into review queue → Same approval flow

Question → Search stored transcripts + extractions →
  → Answer with citations, or "not found"

Scheduler → End of day → Digest from approved items only → File
```

## Your stack

- Python + FastAPI (you know this cold from Lune AI)
- SQLite (zero setup, brief recommends it)
- Pydantic for structured output validation (mandatory, the brief says so)
- Gemini free tier for the LLM (you've used it, good at JSON output, free)
- Streamlit for the review UI (fastest path, polish doesn't matter)
- SQLite FTS5 for search (the brief literally says keyword search that works beats vector store that isn't evaluated)
- APScheduler for the scheduler
- pytest for tests
- Docker + docker-compose for packaging

## The sample data you need to create

You generate this yourself (with an LLM is fine, say so in README).

Four transcripts:
1. Sprint planning, 4-5 speakers, one who never talks, planted difficulties
2. Client status call, 3-4 speakers, a proposed-then-deferred decision
3. A meeting with consent=false (content irrelevant, never processed)
4. A deliberately broken file (truncated, missing speakers, bad encoding)

One chat export:
- 80-100 messages across 3 channels
- 2 project channels, 1 DM thread (excluded)
- 20 messages hand-labelled for testing

Planted difficulties (they specifically ask for these):
- 2 actions with no owner stated
- 1 action with a relative date only ("by end of next week")
- 1 decision proposed then explicitly deferred
- 2 people with similar first names
- 1 action stated by someone on behalf of another person
- 1 long digression with zero commitments

## The golden test cases (your eval harness runs these)

1. Action recall: did you find at least 70% of the hand-labelled actions?
2. Quote integrity: is every quote a literal substring of the transcript? Target: ZERO fabricated quotes
3. Owner accuracy: where an owner IS named, did you get the right person? (target 0.9). Where no owner is named, did you say UNSPECIFIED? (must be 2/2)
4. Date discipline: did you invent any dates? Target: zero
5. Deferred decision test: the deferred item must NOT be in the decision log
6. Retrieval: 5 questions answered correctly, 1 unanswerable returning not-found
7. Chat signal precision: at least 0.7 on the labelled subset, zero DM records
8. Approval gate: API-level test that pending/rejected writes fail

## What makes this connect to Lune AI

You've already built the core patterns this challenge needs:
- Chunking strategy (RecursiveCharacterTextSplitter in Lune AI, segment-based here)
- LLM calls with structured output and retry
- Groundedness checking (Lune AI does this, here it's quote verification)
- FastAPI service layer
- Clean separation between retrieval, extraction, and API

The new parts:
- Approval queue as a first-class data model concept
- Adapter pattern with mock implementations
- Evaluation harness against golden test cases
- Scheduled jobs
- The specific domain (meeting transcripts, not knowledge base)

## Day-by-day plan

Day 1: Setup, ingest transcripts, consent gate. End of day: a transcript is in your DB as segments, consent=false is refused.

Day 2: THE CRITICAL DAY. Action extraction with quote verification and retry. Crude review queue. First eval harness cases running. End of day: a transcript goes in, verified actions appear in a review queue, harness prints its first recall number.

Day 3: Complete approval queue, tracker adapter + mock, writes with idempotency. End of day: approve -> one tracker item, reject -> none, re-run -> no duplicates.

Day 4: Decisions, risks, question answering. End of day: golden questions resolve, deferred decision test passes.

Day 5: Chat signals, scheduler, outcome records. If time is short, cut M11 and say so.

Day 6: Run full harness, fix the worst failure, documentation.

Day 7: Demo recording only. Do not add features.
