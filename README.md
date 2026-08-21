# Meeting & Channel Intelligence Agent

Turns meeting transcripts and chat exports into traceable, human-approved
records. Every extracted action, decision and risk is anchored to a verbatim
quote from its source. Nothing reaches an external system until a person
approves it.

Built for the DigitalT3 intern selection challenge. The brief is committed at
[`docs/challenge/`](docs/challenge/).

> **Status: Phase 2 of 12 complete.** This README is rewritten from the code at
> the end of every phase. The table below reports what runs today, not what is
> planned. Nothing is marked Done until it runs end to end on the sample data.

---

## Capability status

| ID  | Capability                       | Priority | Status    | Note |
|-----|----------------------------------|----------|-----------|------|
| M1  | Ingest and normalise a source    | MUST     | Partial   | txt, vtt and json transcripts done and tested. Audio transcription at Phase 9 |
| M2  | Consent gate                     | MUST     | Done      | Enforced on metadata before the file is opened, again before any model call, and again by database trigger |
| M3  | Extract action items             | MUST     | Done      | Measured. Recall 0.92, zero fabricated quotes, zero invented dates |
| M4  | Extract decisions                | MUST     | Done      | Measured. All 3 golden decisions found, the proposed-then-deferred item correctly absent |
| M5  | Extract risks and blockers       | SHOULD   | Done      | Measured. Both golden risks found, every high severity defensible from its quote |
| M6  | Review and approval queue        | MUST     | Done      | Enforced in the service layer, by database trigger, and proven against raw SQLite with no Python in the path |
| M7  | Write approved items to tracker  | MUST     | Done      | Approve three, re-run twice, exactly three items. Every attempt logged, blocked ones included |
| M8  | Cross-source question answering  | MUST     | Not built | Phase 6. FTS5 index is populated at ingestion |
| M9  | Chat signal classification       | SHOULD   | Not built | DM exclusion enforced by schema, no parser yet |
| M10 | Scheduled end-of-day digest      | SHOULD   | Not built | Phase 8 |
| M11 | Structured outcome record        | SHOULD   | Not built | Phase 8 |
| M12 | Follow-up message draft          | COULD    | Not built | Phase 8 |
| M13 | Per-person digest                | COULD    | Not built | Phase 8 |

---

## Measured quality

Produced by `make eval`, committed at [`eval/results.txt`](eval/results.txt).
Reproduce with `make eval-fresh`, which bypasses the response cache, or
`make eval-repeat` for three uncached runs reported as a range.

> **Free-tier limit, and the workaround.** `gemini-3.6-flash` allows **20
> requests per day** on the free tier. One evaluation run costs six. A
> three-run measurement exhausts the day's quota, which is how this was
> discovered. The response cache is the workaround: `make eval` reuses cached
> responses so a re-run is free, and the cache key covers the prompt version so
> editing a prompt always misses it. `make eval-fresh` deliberately does not
> cache and should be run once per prompt revision, not casually.
>
> The committed results are a **single** run for that reason. The harness
> supports `--runs N` and reports the worst run rather than the average, but
> running it meaningfully needs a quota this build does not have.

**gemini-3.6-flash. 13 hand-labelled actions, 3 decisions, 2 risks and 1
proposed-then-deferred decision across the two original transcripts.**

| # | Metric | Measured | Target | |
|---|---|---|---|---|
| 1  | Action recall | **0.92** | ≥ 0.70 | pass |
| 1b | Precision | 0.71 | reported | see below |
| 2  | **Fabricated quotes** | **0** | 0 | pass |
| 3a | Owner accuracy where named | 0.90 | ≥ 0.90 | pass |
| 3b | UNSPECIFIED compliance | 2/2 | all | pass |
| 4  | Invented dates | **0** | 0 | pass |
| 4b | Relative dates resolved | 5 | reported | each carries the rule that produced it |
| 5  | **Deferred items recorded** | **0** | 0 | pass |
| 5b | Decision recall | **1.00** | ≥ 0.70 | pass |
| M5 | Risk recall | **1.00** | ≥ 0.70 | pass |
| M5b | Severity defensible from the quote | 4/4 | all | pass |

Case 5 is the one the brief singles out. A system that finds every decision and
*also* records the deferral has not passed: it has recorded something that never
happened, and a reviewer cannot tell, because the quote will be perfectly
genuine.

A separate transcript, written independently of this build and never used to
tune a prompt, scores **recall 0.83, precision 1.00, zero fabricated quotes,
zero invented dates, owner accuracy 1.00**. Run it with
`make eval-source SOURCE=meeting-hotel-kickoff-2026-09-15`.

### What the harness found, and what was done about it

The first real run failed two targets and mismeasured a third. All three are in
the git history as one commit.

**The closing recap was re-extracted as new commitments.** A meeting ending
"so to recap, Priya is finishing the auth refactor, James is setting up the
pipeline" restates every commitment thousands of characters from where it was
made, and span-based deduplication cannot see that far. Five of ten false
positives. Fixed in two places: the deduplicator gained a rule for the same
named owner committing to the same task anywhere in the meeting, and the prompt
now says a recap is not a new commitment.

**A date stated for one commitment was attached to another.** "I'll send out a
poll by end of day. Lisa to coordinate the demo scheduling" gave the
coordinating work the poll's deadline. Two invented dates. Prompt v2 states the
timing must belong to this commitment and uses that passage as its example.
Invented dates went to zero. The date rules were left alone: weakening them to
make the metric pass would have been gaming the measurement.

**The harness mis-assigned matches.** A single greedy pass let an early golden
action claim an extraction on the weak signal that a later one would have
claimed on the strong signal. Pairing is now two-pass, quote overlap first.

| | before | after |
|---|---|---|
| Recall | 0.85 | **0.92** |
| Precision | 0.52 | **0.63** |
| Invented dates | 2 (fail) | **0** (pass) |
| UNSPECIFIED compliance | not measured | **2/2** (pass) |
| Owner accuracy | 1.00 | 0.90 |

**The harness found an error in the golden set, twice.** In Phase 3 the client
status call's metadata date contradicted its own content. In Phase 5 three
actions were labelled as having no due date when the transcript plainly states
one: *"I'll run the baseline tests this week"* was recorded as UNSPECIFIED, and
the system resolving it was scored as an invented date for three runs running.

Correcting a label is only legitimate when the correction can be justified from
the transcript alone, with no reference to what any model produced. All three
could be. Having found one, every remaining UNSPECIFIED date label was audited
the same way and two more were found; two others were checked and deliberately
left alone, because the timing near them belongs to a different commitment.
Details in [`decision_log.md`](decision_log.md).

### The weakest part of this build

**Precision of 0.63 is the number to be sceptical about, and it is not what it
looks like.** Reading all seven remaining false positives by hand: six are
genuine commitments that the hand-labelled golden set simply does not contain
("I'll send the notes around within the hour", "I'll share the link in the team
channel", "I'll send you her contact details"). One is a matching artefact: the
golden set quotes a request and the model quoted the acceptance of it.

So the measured precision is a lower bound, and the golden set is incomplete
rather than the extractor being noisy. **Those six were deliberately not added
to the golden set**, because labelling ground truth after seeing what the model
produced is fitting the labels to the output. With more time the right fix is a
second person labelling both transcripts blind.

**Owner accuracy fell from 1.00 to 0.90 with prompt v2 and is reported as such.**
The single failure returns UNSPECIFIED for a commitment whose owner the golden
set names. An abstention where attribution was possible is the safe direction to
be wrong in, but it is still wrong.

**Gemini is not deterministic at temperature 0, so a single run is a sample.**
Two runs minutes apart over the same chunk returned different action sets. A
later re-run scored owner accuracy 1.00 and invented dates 1, against 0.90 and
0 in the committed run. Recall, precision and the fabricated-quote count were
identical in both.

That variance is the honest reason `make eval-repeat` exists and why it counts a
target as met only when the *worst* run met it. It is also why the committed
file is one run and says so: the free tier's 20 requests a day cannot pay for
three.

**A quota-exhausted run once overwrote a good results file with meaningless
numbers.** Every chunk failed with a 429, the harness scored the empty result at
recall 0.00 and wrote it out. Committing that would have been fabricated
evaluation results, which is an automatic failure in this exercise. The harness
now refuses to write anything when any chunk failed, exits non-zero, and says
which committed file it left alone. Three tests cover it, including one proving
that a *single* transient 429 is absorbed by the retry loop and does not
invalidate a run.

---

## What exists right now

```
backend/app/db/schema.sql            13 tables, 3 FTS5 indexes, 20 triggers
backend/app/config.py errors.py      typed settings, domain errors
backend/app/db/                      connection, transaction, repositories
backend/app/models/                  Pydantic contracts
backend/app/ingestion/               M1 parsers, M2 consent gate, validation
backend/app/extraction/
  prompts.py                         versioned prompt loading
  chunker.py                         segment-boundary chunks with context
  quote_verifier.py                  the substring check and where the quote sits
  dates.py                           relative dates, anchored to the meeting
  deduplicator.py                    two rules, within a region and across one
  actions.py                         M3, the extraction pipeline
  llm/                               two providers, one interface, retry, cache
backend/app/prompts/*.txt            one versioned file per capability
backend/app/review/queue.py          M6 rules: edit, approve, reject, expire
backend/app/routers/                 thin HTTP: sources, extractions, review
frontend/                            React 19 + TypeScript + Tailwind
eval/harness.py golden.py            the golden cases and the scoring
backend/tests/ eval/test_harness.py  209 passing tests
scripts/                             seed, check-env, llm-smoke, fixtures
sample_data/                         4 transcripts, 1 chat export, 5 golden files
```

Try it:

```bash
make seed                     # ingests, refuses and rejects, and says which
make check-env                # which providers are reachable, which prompts exist
make llm-smoke PROVIDER=fake  # the whole model path offline, no key needed
make eval                     # the golden cases against the live model
make run                      # API on :8000
make ui                       # review interface on :5173
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
against someone opening the file with the `sqlite3` CLI.

**The consent gate fires on metadata, before the file is opened.** A refused
source's ingestion report carries `bytes_read: 0`, which is machine-checkable
evidence the content was never read, parsed or sent to a model.

**Ambiguity is surfaced, never resolved by guessing.** An unlabelled transcript
line keeps `speaker: null` and records a warning saying why. A first name shared
by two participants is left unresolved. `UNSPECIFIED` is a first-class typed
value, so the model always has a correct thing to output.

**Malformed files are graded, not binary.** Truncation, undecodable bytes and an
unrecognisable format reject the whole file. A missing speaker label travels
with the source as a warning.

**Structured output is constrained at the decoder, then validated anyway.**
Gemini receives the JSON schema through `response_json_schema`; Ollama receives
the same schema in its `format` field. Every response is still validated
against the Pydantic contract, because a constrained decoder makes malformed
output unlikely rather than impossible.

**Failures feed the actual error back to the model.** A missing field produces
`owner: Field required`. A rejected quote produces the quote that failed and an
instruction to use exact text. Quote verification plugs into the same loop as a
validator, so a fabricated quote is repaired rather than merely rejected.

**Every attempt is accounted for.** One row per attempt in `llm_calls`, grouped
by a logical call id, so the retry rate, the cache hit rate and the per-source
token cost are measured rather than estimated.

**Prompts are versioned files with a content hash.** One file per capability
with a declared version, plus a SHA-256 of the body. Both are recorded on every
extraction, so an edit made without bumping the version still changes the tag
and a measured result can never be attributed to the wrong prompt.

**Chunking is by whole segments, with whole-segment overlap and a context
header.** A segment is one person's turn, so splitting it would separate a
commitment from the words that make it one. A commitment is usually made across
two turns, so overlap keeps the pair intact in at least one chunk. The context
header names every participant, because without it the model cannot tell that
"James" is James Liu, or that two people called Priya are in the room.

**Two providers, one interface.** Gemini and Ollama both implement
`LLMProvider`, and the factory is the only place that knows the difference.
The adapter contract asks whether a real integration could be dropped in by
writing one class and changing one line of wiring. Having written the second
class is the honest way to answer that.

**Nothing reaches the tracker without approval, and it is enforced three times
over.** The service layer refuses and explains; `trg_approval_gate_write` refuses
the audit row; `UNIQUE (tracker_writes.extraction_id)` makes a duplicate
impossible rather than unlikely. `eval/test_approval_gate.py` proves all three,
calling the service directly and then going lower still to raw SQLite with no
Python in the path, which is exactly the "bypassable via the API" red flag.

**The mock tracker starts with a backlog it did not create.** Twelve seeded
tickets with missing assignees, free-text statuses (`"In Progress "` with
trailing whitespace sits beside `"In Progress"`), due dates already in the past,
and a pair of near-duplicates for the same bug raised twice. The contract says
an agent that only works on clean data has not been tested. `TrackerItem`
deliberately does not strip whitespace: our own contracts normalise, foreign
data is kept as it was found.

**A written ticket carries its evidence.** The verbatim quote, who said it,
when, which meeting, the extraction id, and the rule that resolved any date. An
UNSPECIFIED owner becomes an unassigned ticket labelled `needs-owner`, never a
guess.

**One definition of "the text of a source".** Segments joined by single spaces
after whitespace normalisation. Quote verification checks against that string
and every character offset indexes into it, so a citation points at a location
inside a source and can be checked by hand.

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
