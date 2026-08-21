# Testing and evaluation

Two different questions are being answered here, and conflating them is the
mistake the brief warns about:

> "Unit tests prove the code runs. They say nothing about whether the
> extraction was right, the summary was grounded, or the prioritisation was
> sane. Almost every submission has tests; very few have evals."

| | Question | Where | Run by |
|---|---|---|---|
| **Test suite** | Does the code do what it is supposed to do? | `backend/tests/`, `eval/test_harness.py`, `eval/test_approval_gate.py` | `make test` |
| **Eval harness** | Is the model's output any good? | `eval/harness.py` | `make eval` |

The suite never asserts a quality number and the harness never asserts a code
path. Keeping them apart is what stops a green suite being mistaken for a good
system.

```
make test            278 tests, ~8 seconds, no network, no API key
make eval            the golden cases against the live model
make eval-fresh      the same, cache bypassed
make eval-repeat     three uncached runs, reported as a range
make eval-source SOURCE=<id>   one source against its golden labels
```

---

## Techniques used, and why

### 1. Test doubles are real implementations, not mocks

`FakeProvider`, `HashingEmbedder` and `MockTracker` all implement the same
interface as the thing they stand in for, and are selected by the same factory
from the same configuration value.

They are not mocks. `FakeProvider` goes through the whole wrapper: schema
construction, JSON parsing, Pydantic validation, quote verification, the repair
loop, the response cache and the telemetry write. `HashingEmbedder` produces
genuine unit vectors with real cosine geometry, so FAISS, the search and the
fusion are all exercised for real.

**Why it matters:** it is the only way to test the failure paths. A live model
cannot be made to return malformed JSON, or a fabricated quote, or the same
deferred decision twice, on demand. Scripting the failure is not a shortcut
around testing the retry loop, it is the only way to test it at all.

### 2. Every negative test is proved able to fail

A negative test that cannot fail proves nothing. Golden case 5 asserts a
deferred decision does **not** reach the decision log — which would also pass if
extraction silently did nothing.

So the fixture takes `include_deferred=True`, scripts a model that records the
deferral, and a second test asserts it **does** then reach the store.

Same pattern elsewhere: the harness tests deliberately break the scoring
(dropping actions, guessing owners, inventing dates, fabricating quotes) and
assert each metric moves in the right direction and names the right item.

### 3. The boundary of the test double is pinned as a test

`test_the_stub_cannot_pass_the_not_found_case` asserts that the scripted
provider **fails** golden case 6b.

The stub answers by lexical overlap. That exercises retrieval, citation
verification and scoring, and cannot tell *"what database did we decide to use
for the user analytics module"* — which the corpus does not answer — from
*"what database"*, which it does.

Case 6b measures that judgement, so it needs a real model, and its README
number comes from one. Pinning the failure stops somebody later tuning the stub
until it passes and believing the suite covers 6b.

### 4. Rules are tested one layer below where they are enforced

The approval gate lives in the service layer, in a database trigger, and in a
unique constraint. The rubric's red flag is gating that *"exists in the UI but
is bypassable via the API"*.

So `eval/test_approval_gate.py` never goes through HTTP. It calls the service
directly, then goes lower still and opens raw SQLite with no Python service in
the path, and asserts each forbidden write is refused:

```python
with pytest.raises(sqlite3.IntegrityError, match="approval_gate"):
    conn.execute("INSERT INTO tracker_writes ...", (pending_id, ...))
```

`backend/tests/test_schema_guarantees.py` does the same for the consent gate,
terminal review states, the immutable original payload, the append-only audit
tables and DM exclusion — 19 tests, all against the production `schema.sql`, so
removing a trigger breaks the suite.

### 5. Metrics are recomputed, never read back

The fabricated-quote count re-checks each stored quote against the stored
transcript rather than trusting the `quote_verified` flag. The most important
number in the submission must not depend on the code path that set it.

### 6. Tests assert properties, not sample-set totals

Three tests once hard-coded "106 segments" and an exact set of source ids.
Adding a fifth transcript broke them for a reason unrelated to what they tested.

They now assert the property: every ingested source stores exactly the segments
its report claims, every refused or errored source stores none, and the FTS
index matches the segment count whatever it is.

### 7. Warnings are errors

`filterwarnings = error` in `pytest.ini`, with one named third-party exception.
A deprecation in our own code fails the build rather than scrolling past.

`pytest-asyncio` was removed rather than configured to silence its warning:
nothing in the suite is asynchronous, and an unused dependency is a claim about
the build that is not true.

### 8. The suite does not sleep, download or call out

- Backoff base is `0` in the test environment. Rate-limit behaviour is still
  asserted; the waiting is not. This took the suite from 44 seconds back to 8.
- The hashing embedder means no model download and no torch load.
- Every database is a temporary one built from the real `schema.sql`.

### 9. Ground truth is guarded by a test

`test_every_golden_quote_is_a_substring_of_the_normalised_source_text` asserts
every hand-labelled quote verifies against the exact string the pipeline uses.

If the definition of "the text of a source" ever drifts from the golden set,
this fails **before** the harness reports a misleading number.

### 10. The harness refuses to produce a result it cannot stand behind

Three separate refusals:

- **Stub run** → prints `NOT A MEASUREMENT`, writes no results file
- **Incomplete run** (any chunk failed, usually an exhausted quota) → prints
  `INCOMPLETE`, writes nothing, exits 2, names the file it left alone
- **`--sources`** → prints results, writes nothing, because `eval/results.txt`
  describes one fixed corpus

This was written after a three-run evaluation exhausted the daily quota, scored
the empty result at recall 0.00, and **overwrote a good results file with it**.
Committing that would have been fabricated evaluation results.

### 11. Ground truth is corrected only when the transcript says so

Labelling after reading model output is fitting labels to output. The test
applied before any label may change: **can the correction be justified from the
transcript alone?**

When `action_sp_08` was reported as an invented date three runs running, the
label was wrong — Marcus says *"I'll run the baseline tests this week"*. Having
found one, every remaining `UNSPECIFIED` date label was audited the same way.
Two more were wrong; **two were checked and deliberately left alone**.

New labels are committed **before** the source is extracted, so git shows they
predate the run. See commit `3a0226b`.

---

## What is covered

| Area | Tests | File |
|---|---|---|
| Database-enforced rules | 19 | `test_schema_guarantees.py` |
| M2 consent gate | 8 | `test_consent_gate.py` |
| M1 parsing, three formats | 18 | `test_transcript_parsers.py` |
| M1 validation and storage | 20 | `test_ingestion_pipeline.py` |
| HTTP surface | 12 | `test_api_sources.py` |
| LLM wrapper, retry and repair | 17 | `test_llm_client.py` |
| Provider swappability | 10 | `test_llm_providers.py` |
| Prompts and chunking | 13 | `test_prompts_and_chunking.py` |
| Quote verification, date discipline | 33 | `test_quote_and_dates.py` |
| M3 extraction end to end | 21 | `test_action_extraction.py` |
| M4 decisions, golden case 5 | 11 | `test_decision_extraction.py` |
| M5 risks, severity defensibility | 10 | `test_risk_extraction.py` |
| M6 review queue | 21 | `test_review_queue.py` |
| M7 tracker adapter and mock | 21 | `test_tracker_adapter.py` |
| **Golden case 8, approval enforcement** | 19 | `eval/test_approval_gate.py` |
| Harness scoring | 21 | `eval/test_harness.py` |

## What is not covered, and why

- **Retrieval quality** is measured by the harness against MiniLM, never
  asserted in the suite. The hashing embedder has no notion of meaning, so a
  quality assertion using it would be meaningless.
- **Model judgement** — golden cases 6b and 6d need a real model. The stub's
  inability is pinned as a test rather than papered over.
- **The React interface** has no automated tests. A deliberate cut: the brief
  awards no marks for the interface and the review surface is exercised through
  the API tests underneath it. Recorded in `decision_log.md`.
- **Ollama** has never been run against a live daemon on this machine. The
  class is written and its unreachable path is tested. `L8`.
