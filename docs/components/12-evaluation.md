# 12 · Evaluation

**Scored separately, and worth more of the total than the interface.**
**Code:** `eval/harness.py`, `eval/golden.py`
**Tests:** 43 across `eval/test_harness.py` (24) and `eval/test_approval_gate.py` (19)

```bash
make eval                      the golden cases against the live model
make eval-fresh                the same, cache bypassed
make eval-repeat               three uncached runs, reported as a range
make eval-source SOURCE=<id>   one source against its golden labels
```

---

## Results

`gemini-3.6-flash`, prompt `extract_actions` v2. Committed at
[`../../eval/results.txt`](../../eval/results.txt).

| # | Metric | Measured | Target | |
|---|---|---|---|---|
| 1 | Action recall | **0.92** | ≥ 0.70 | pass |
| 1b | Precision | 0.71 | reported | see below |
| 2 | **Fabricated quotes** | **0** | 0 | pass |
| 3a | Owner accuracy where named | 0.90 | ≥ 0.90 | pass |
| 3b | UNSPECIFIED compliance | 2/2 | all | pass |
| 4 | Invented dates | **0** | 0 | pass |
| 5 | **Deferred items recorded** | **0** | 0 | pass |
| 5b | Decision recall | 1.00 | ≥ 0.70 | pass |
| M5 | Risk recall | 1.00 | ≥ 0.70 | pass |
| M5b | Severity defensible | 4/4 | all | pass |
| 6 | Retrieval, correct source | 5/5 | 5/5 | pass |
| 6b | **Not-found on the unanswerable** | 1/1 | all | pass |
| 6d | Answers carrying a verified citation | 5/5 | all | pass |
| 7 | Chat signal precision | 0.87 | ≥ 0.70 | pass |
| 7b | Direct messages in the store | **0** | 0 | pass |
| 8 | Approval enforcement | 19 tests | all fail correctly | pass |

An **independent transcript**, written by the candidate about a different domain
and never used to tune a prompt: recall 0.83, **precision 1.00**, zero
fabricated quotes, zero invented dates, owner accuracy 1.00.

---

## Design principles

### 1. The harness refuses to produce a result it cannot stand behind

Three separate refusals:

| Condition | Behaviour |
|---|---|
| Stub provider | `NOT A MEASUREMENT` banner, **no results file** |
| Any chunk failed | `INCOMPLETE` banner, **no results file**, exit 2, names the file it left alone |
| `--sources` given | Results printed, **nothing written** — `results.txt` describes one fixed corpus |

**This was written after it happened.** A three-run evaluation exhausted the
Gemini free tier's daily cap, every chunk failed with a 429, and the harness
scored the empty result at recall 0.00 and **overwrote a good results file with
it.** Committing that would have been fabricated evaluation results — an
automatic failure.

A test pins the distinction: **a single transient 429 absorbed by the retry loop
is not a failure**, so the guard cannot become over-eager.

### 2. Metrics are recomputed, never read back

The fabricated-quote count re-checks each stored quote against the stored
transcript rather than trusting `quote_verified`. **The most important number in
the submission must not depend on the code path that set it.**

### 3. The matching rule is stated, not buried

> An extraction matches a golden action when their quotes overlap — one being a
> substring of the other after normalisation — or failing that when their task
> descriptions share at least half their content words.

Quote overlap is primary because **the quote is the anchor**. The task fallback
exists because the model may quote a neighbouring sentence of the same exchange.

**Two passes**, quote overlap first across all golden actions, then the task
fallback. A single greedy pass let an early golden action claim an extraction on
the *weak* signal that a later one would have claimed on the *strong* one —
understating recall with no extraction being wrong.

**One-to-one throughout**, so a single vague extraction cannot claim credit for
two commitments.

**The rule is deliberately generous**, which inflates recall and deflates false
positives. So both are printed side by side and **neither is quoted alone.**

### 4. Reported alongside, because the brief asks

- **False positives and precision** — *"a system that extracts thirty actions to
  catch ten is not useful"*
- **Precision and recall at five confidence thresholds** — the calibration
  stretch, obtained almost free once the pairing exists
- **Retry, cache and token statistics** read from `llm_calls`
- **All three retrieval modes**, at source and segment granularity

---

## What the harness found, and what was done

The brief: *"A harness that reveals a weakness you then explain scores higher
than a harness that reports everything passing."*

### Run 1 — two targets failed

**The closing recap was re-extracted as new commitments.** Five of ten false
positives. Fixed with a second deduplication rule *and* a prompt revision.

**A date stated for one commitment was attached to another.** Two invented
dates. Fixed in prompt v2. **The date rules themselves were left alone** —
weakening them to make the metric pass would have been gaming the measurement.

**The harness itself mis-assigned matches.** Fixed with two-pass pairing.

| | before | after |
|---|---|---|
| Recall | 0.85 | **0.92** |
| Precision | 0.52 | **0.71** |
| Invented dates | 2 (fail) | **0** (pass) |
| UNSPECIFIED compliance | not measured | **2/2** (pass) |
| Owner accuracy | 1.00 | 0.90 |

### The golden set was wrong, twice

**Phase 3:** the client status call's metadata date said 18 November while the
transcript is internally set in mid-August. Every relative date from that source
resolved against the wrong anchor.

**Phase 5:** three actions were labelled as having no due date when the
transcript plainly states one. *"I'll run the baseline tests this week"* was
recorded as `UNSPECIFIED`, and the system resolving it was scored as an invented
date **for three runs running.**

**The test applied before any label may change:** can the correction be
justified **from the transcript alone**, with no reference to what any model
produced?

All three could. Having found one, **every** remaining `UNSPECIFIED` date label
was audited the same way. Two more were wrong. **Two were checked and
deliberately left alone** — `action_cs_05`, where *"by end of day"* belongs to
the poll rather than the coordination, and `action_hk_06`, where *"talk
tomorrow"* is a farewell.

Only one moved a metric. **The correction that mattered is the one where the
model was right.**

New labels are committed **before** the source is extracted, so git shows they
predate the run. Commit `3a0226b`.

---

## The weakest parts, stated

### Precision 0.71 is a lower bound

Reading the remaining false positives by hand: most are **genuine commitments
the golden set does not contain** (*"I'll send the notes around within the
hour"*, *"I'll share the link in the team channel"*).

**Those were deliberately not added.** Labelling ground truth after seeing model
output is fitting labels to the output. With more time the right fix is a second
person labelling both transcripts blind.

### Hybrid does not beat dense

| Mode | Source | Segment | Mean rank |
|---|---|---|---|
| keyword | 5/5 | 4/5 | 2.4 |
| dense | 5/5 | **5/5** | **2.0** |
| hybrid | 5/5 | 4/5 | **2.0** |

The brief's metric **saturates**. On the stricter one dense wins by one
question. Hybrid is kept on an argument that is **not measured**, and **on
today's evidence dense would win.**

### The model's confidence is decoration

The calibration table is flat: Gemini returns **0.95 for almost everything**.
Prompt v2 explicitly says *"do not return 0.95 for everything"* and it made no
difference.

### Gemini is not deterministic at temperature 0

Two runs minutes apart returned different action sets. Owner accuracy moved 0.90
↔ 1.00 and invented dates 0 ↔ 1; recall, precision and the fabricated-quote
count were stable.

`make eval-repeat` runs N times uncached and counts a target as met **only when
the worst run met it.** It is not used for the committed numbers because **the
free tier's 20 requests a day cannot pay for three runs**, and the README says
so.

---

## The golden data

| File | Contents |
|---|---|
| `golden_actions.json` | 19 hand-labelled actions across three transcripts |
| `golden_decisions.json` | 9 decisions, **2 proposed-then-deferred** |
| `golden_risks.json` | 4 risks with severity |
| `golden_questions.json` | 5 answerable, **1 genuinely absent** |
| `golden_signals.json` | 20 labelled messages, **12 forbidden DM ids** |

Every quote in every golden file is asserted to be a literal substring of the
transcript it came from — so ground truth cannot silently drift from the system
measured against it.

**Planted difficulties** the brief asks for: two ownerless commitments, a
relative date, a proposed-then-deferred decision, two people sharing a first
name, an action stated on behalf of another, a long digression with no
commitments, and a participant who never speaks.

---

## What it does not do

- **No inter-annotator agreement.** One person labelled everything. `L9`.
- **No statistical confidence intervals.** 13 actions, 3 decisions, 2 risks and
  20 messages are small samples, and `L18` and `L23` say so.
- **No cost projection.** `llm_calls` holds the raw tokens and latency; nothing
  extrapolates.
