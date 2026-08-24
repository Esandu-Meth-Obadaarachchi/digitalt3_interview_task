# Meeting & Channel Intelligence Agent

Turns meeting transcripts and chat exports into traceable, human-approved
records. Every extracted action, decision and risk is anchored to a verbatim
quote from its source. Nothing reaches an external system until a person
approves it.

Built for the DigitalT3 intern selection challenge. The brief is committed at
[`docs/challenge/`](docs/challenge/).

> **Status: all 13 capabilities run end to end.** 7 MUST, 4 SHOULD, 2 COULD. This README is rewritten from the code at the end
> of every phase. The table below reports what runs today, not what is planned.
> Nothing is marked Done until it runs end to end on the sample data.

---

## Capability status

| ID  | Capability                       | Priority | Status    | Note |
|-----|----------------------------------|----------|-----------|------|
| M1  | Ingest and normalise a source    | MUST     | Done      | txt, vtt, json, chat exports **and audio**. Whisper runs in a worker process, produces no speaker labels, and says so on every record |
| M2  | Consent gate                     | MUST     | Done      | Enforced on metadata before the file is opened, again before any model call, and again by database trigger |
| M3  | Extract action items             | MUST     | Done      | Measured. Recall 0.77, precision 0.59, zero fabricated quotes, zero invented dates |
| M4  | Extract decisions                | MUST     | Done      | Measured. Recall 0.67, **below the 0.70 target**. The deferred item correctly absent |
| M5  | Extract risks and blockers       | SHOULD   | Done      | Measured. Recall 1.00, every severity defensible from its quote |
| M6  | Review and approval queue        | MUST     | Done      | Enforced in the service layer, by database trigger, and proven against raw SQLite with no Python in the path |
| M7  | Write approved items to tracker  | MUST     | Done      | Approve three, re-run twice, exactly three items. Every attempt logged, blocked ones included |
| M8  | Cross-source question answering  | MUST     | Done      | Measured. 5/5 correct source, 5/5 carrying a verified citation, refuses the unanswerable |
| M9  | Chat signal classification       | SHOULD   | Done      | Measured. Precision 0.93, zero direct-message records. An export can be uploaded, not only seeded |
| M10 | Scheduled end-of-day digest      | SHOULD   | Done      | Real APScheduler, two jobs, clock override. Approved items only, every line cited |
| M11 | Structured outcome record        | SHOULD   | Done      | Versioned, approved items only, schema published at docs/outcome_schema.json |
| M12 | Follow-up message draft          | COULD    | Done      | Rendered from approved items, never written by the model. A person edits and sends it. A blank or service `sent_by` is refused four ways |
| M13 | Per-person digest                | COULD    | Done      | Cross-source, uncapped. Nobody with no commitments gets one. Unowned work gets its own digest saying the assignee is unspecified |

---

## Measured quality

Produced by `make eval` on **24 August 2026**, `gemini-3.6-flash`, prompt
`extract_actions v2+44157f`. Committed at [`eval/results.txt`](eval/results.txt)
and [`results.json`](eval/results.json). Reproduce with `make eval-fresh`, which
bypasses the response cache.

**13 hand-labelled actions, 3 decisions, 2 risks and 1 proposed-then-deferred
decision across the two scored transcripts. 17 actions extracted.**

| # | Metric | Measured | Target | |
|---|---|---|---|---|
| 1  | Action recall | 0.77 | ≥ 0.70 | pass |
| 1b | Precision | 0.59 | reported | see below |
| 2  | **Fabricated quotes** | **0** | 0 | pass |
| 3a | Owner accuracy where named | 0.90 | ≥ 0.90 | pass |
| 4  | **Invented dates** | **0** | 0 | pass |
| 4b | Relative dates resolved | 5 | reported | each carries the rule that produced it |
| 5  | **Deferred item recorded** | **0** | 0 | pass |
| 5b | Decision recall | **0.67** | ≥ 0.70 | **FAIL** |
| M5 | Risk recall | 1.00 | ≥ 0.70 | pass |
| M5b | Severity defensible from the quote | 4/4 | all | pass |
| 6  | Retrieval, correct source in top 3 | 5/5 | 5/5 | pass |
| 6b | **Not-found on the unanswerable** | **1/1** | all | pass |
| 6d | Answers carrying a verified citation | 5/5 | all | pass |
| 7  | Chat signal precision | 0.93 | ≥ 0.70 | pass |
| 7b | **Direct messages in the store** | **0** | 0 | pass |
| 8  | Approval enforcement | 19 tests | all refuse | pass |

**One metric is below target and it is reported as a failure, not rounded
away.** Decision recall is 0.67: two of three, missing `decision_sp_02`. The
harness exits non-zero because of it.

### What these numbers do and do not say

**The three integrity numbers are the ones that matter, and all three are
clean.** Zero fabricated quotes, recomputed against the stored transcript rather
than read from the flag the pipeline set. Zero invented dates. The
proposed-then-deferred decision correctly absent. A system that finds every
decision and *also* records the deferral has not passed: it has recorded
something that never happened, and the quote will be perfectly genuine.

**Precision 0.59 is a lower bound.** Reading the seven false positives by hand,
most are genuine commitments the golden set does not contain. They were
deliberately not added: labelling ground truth after seeing model output is
fitting labels to the output. The honest fix is a second person labelling both
transcripts blind, and it is not built.

**Recall moved between runs.** The same corpus and prompt measured 0.92 in phase
3 and 0.77 here. Gemini is not deterministic at temperature 0, and the harness
supports `--runs N`, reporting the worst run rather than the average. Running it
meaningfully needs a quota this build does not have: the free tier is 20
requests per day per project and one run costs about eighteen.

### Three things the harness refuses to do

| Condition | Behaviour |
|---|---|
| The provider is the deterministic stub | `NOT A MEASUREMENT` banner, no file written |
| Any chunk **or question** failed | `INCOMPLETE` banner, no file, exit 2, names the file it left alone |
| `--capabilities` names a subset | prints its numbers, writes nothing, names which capabilities scored zero for never having run |
| `--sources` given | prints, writes nothing — `results.txt` describes one fixed corpus |

Each exists because the failure happened. The second overwrote a good results
file with recall 0.00 when a quota ran out. The third wrote a file scoring three
capabilities that never ran, and it stood in the repository for six phases
contradicting this README. On the day these numbers were produced the second
guard refused three runs before one completed, which is the guard working rather
than a problem with the build.

### The retrieval mode comparison

All three modes are scored on every run, at source and at segment granularity,
with neighbour expansion switched off so it cannot flatter every mode equally.
The honest finding from the last complete run was that **hybrid did not beat
dense**: both reached 5/5 at source granularity, and at segment granularity
dense reached 5/5 while hybrid reached 4/5. Hybrid is kept on an argument about
vocabulary mismatch on unseen questions, and that argument is not measured.

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

Measured at the time of that revision, in phase 3. These are historical, not a
current claim: no measurement is committed today, for the reason at the top.

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
backend/app/db/schema.sql            14 tables, 3 FTS5 indexes, 25 triggers
backend/app/config.py errors.py      typed settings, domain errors
backend/app/db/                      connection, transaction, 7 repositories
backend/app/models/                  9 Pydantic contracts, all strict
backend/app/ingestion/               M1 parsers, M2 consent gate, validation
backend/app/audio/                   M1 audio, whisper in a worker process
backend/app/extraction/
  prompts.py                         versioned prompt loading, hash-tagged
  chunker.py                         segment-boundary chunks with context
  quote_verifier.py                  the substring check and where the quote sits
  dates.py                           relative dates, anchored to the meeting
  deduplicator.py                    two rules, within a region and across one
  pipeline.py                        one path shared by M3, M4 and M5
  actions.py decisions.py risks.py   the three extraction specs
  signals.py                         M9, chat classification
  llm/                               three providers, one interface, retry, cache
backend/app/prompts/*.txt            one versioned file per capability
backend/app/review/queue.py          M6 rules: edit, approve, reject, expire
backend/app/tracker/                 M7 writes, idempotent, every attempt logged
backend/app/retrieval/               M8 FTS5 + FAISS, fused by reciprocal rank
backend/app/scheduler/               M10 APScheduler, two jobs, clock override
                                     M13 per-person digests, cross-source
backend/app/people/identity.py       M13 which commitments belong to one person
backend/app/followup/draft.py        M12 the recap draft, and the send refusal
backend/app/agent/                   the tool-dispatch loop: 10 tools, none crossing a gate
backend/app/outcome/record.py        M11 versioned outcome record
backend/app/adapters/                3 interfaces, 3 mocks, one factory
backend/app/routers/                 thin HTTP, 8 routers
frontend/src/                        React 19 + TypeScript + Tailwind, 9 views
eval/harness.py golden.py            the golden cases and the scoring
backend/tests/ eval/                 466 passing tests, counted by make test-inventory
scripts/                             seed, check-env, llm-smoke, verify-clone, fixtures
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

## Documentation

| Document | What it is |
|---|---|
| [`docs/architecture.md`](docs/architecture.md) | The required architecture note: data flow, where each gate is enforced, what runs where, and the limitations |
| [`docs/components/`](docs/components/README.md) | One document per part of the system, 14 in all, each covering what it does, the decisions and their reasons, how it is tested, and what it does not do |
| [`docs/testing.md`](docs/testing.md) | The testing approach, what is a real implementation rather than a mock, and the count per file |
| [`decision_log.md`](decision_log.md) | Every decision in order, with the reason and the alternative rejected |
| [`eval/results.txt`](eval/results.txt) | The committed measurement, reproducible with `make eval` |

Start with the architecture note. [`docs/components/README.md`](docs/components/README.md)
gives a 20-minute reading order through the rest.

---

## Run it with Docker

The shortest path, and the one that needs nothing installed but Docker and a
Gemini key.

```bash
cp .env.example .env          # then add GEMINI_API_KEY
make docker-up                # builds both images and starts them
make docker-seed              # load the committed sample data
```

Interface on <http://localhost:5173>, API on <http://localhost:8000/health>.

```bash
make docker-test              # the whole suite, inside the image
make docker-seed-empty        # schema and the tracker backlog only, for your own data
make docker-down              # stop, keeping the store
make docker-clean             # stop and delete the store, logs and model cache
```

Two services. The API holds every rule. The interface is built to static files
and served by nginx, which proxies `/api` and `/health` through, so the browser
talks to one origin and CORS never comes into it. Three volumes: the store, the
write logs, and the model cache. Everything else is rebuilt from the image,
which is what makes a run reproducible.

`make docker-test` is the check worth having. `make verify-clone` proves the
repository holds everything; running the suite inside the image proves the
dependencies do.

Two notes on the build. The image installs **CPU-only torch explicitly**,
because `sentence-transformers` pulls torch and the default index serves the
CUDA build, roughly 2.5GB of NVIDIA libraries for a container with no GPU. The
MiniLM embedding model is **baked into the image** so retrieval works with no
network; whisper's weights are not, since audio is optional and its model is
140MB, so it caches to a volume on first use.

---

## Setup without Docker

Requires Python 3.11 (newest version with settled wheels for `faiss-cpu`,
`sentence-transformers` and `ctranslate2` on Apple Silicon) and Node 20+.

```bash
cp .env.example .env          # then add GEMINI_API_KEY
make setup                    # virtualenv + dependencies
make seed                     # rebuild the store from schema.sql
make test                     # run the test suite
make check-env                # show which providers are configured
```

`make run` starts the API, `make ui` the review interface. `make eval` runs the
golden cases against the live model and refuses to write a results file if any
chunk failed. `make verify-clone` clones the current branch into a temporary
directory and runs the whole suite there, so a missing file cannot pass locally
and fail for a reviewer.

---

## Design decisions

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
