# 04 · Extraction

**Capabilities:** M3 (actions), M4 (decisions), M5 (risks)
**Code:** `extraction/pipeline.py`, `actions.py`, `decisions.py`, `risks.py`
**Tests:** 42 across `test_action_extraction.py` (21), `test_decision_extraction.py` (11), `test_risk_extraction.py` (10)

---

## One pipeline, three specs

M3, M4, M5 and M9 differ in exactly three things: **which prompt they load,
which contract the model must satisfy, and how a validated item becomes a stored
payload.** Those three are an `ExtractionSpec`. Everything else lives in
`pipeline.py`, once.

```python
ACTION_SPEC = ExtractionSpec(
    extraction_type=ExtractionType.ACTION,
    prompt_name="extract_actions",
    response_model=DraftActionList,
    items_field="actions",
    task_of=lambda item: item.what,
    owner_of=lambda item: item.owner,
    payload_of=action_payload,
)
```

`actions.py` is 58 lines. It was **276** before the pipeline was lifted out.

**Lifted, not predicted.** The deduplication thresholds, the quote-repair
fallback and the accounting were worked out against real model output over two
phases and several of them changed twice. Copying them into three more modules
would have meant four places to fix whatever the harness found next.

---

## The pipeline

```
consent gate ──► chunk ──► per chunk: model call with quote verification
                            inside the retry loop
                                  │
                            collect candidates
                                  │
                            deduplicate (two rules)
                                  │
                            store as PENDING
```

Two layers of contract: `Draft*` is what the model returns, shaped for the
model; `Extraction` is what the store holds, with provenance, verification
state, review state and audit fields. Keeping them apart means the model is
never handed fields it has no business inventing — such as its own review status
or the character offsets of its own quote.

---

## M3 — action items

The capability test: recall ≥ 0.7, no quote that is not a literal substring, and
the two commitments with no stated owner come back as `UNSPECIFIED`.

**`UNSPECIFIED` is a first-class typed value.** The brief names silent guessing
as *"the single most damaging failure mode in a delivery-facing agent, because
it is fluent and therefore trusted"*. Making abstention explicit means the model
always has a correct thing to output and is never cornered into inventing an
owner or a date.

A shared normaliser maps the spellings a model actually returns — `none`, `n/a`,
`not stated`, `no reason given`, `unclear` — onto the single token the rest of
the system checks for. It recognises synonyms of abstention and changes nothing
else.

### Prompt v1 → v2, driven by measurement

The first real run scored **recall 0.85, precision 0.52, invented dates 2**.
Two failures drove v2:

**The closing recap was re-extracted as new commitments.** Five of ten false
positives. Fixed in code (a second deduplication rule) *and* in the prompt (a
recap is not a new commitment).

**A date stated for one commitment was attached to another.** *"I'll send out a
poll by end of day. Lisa to coordinate the demo scheduling"* gave the
coordinating work the poll's deadline. Prompt v2 states the timing must belong
to *this* commitment and uses that exact passage as its example.

**The date rules themselves were left alone.** Weakening them to make the metric
pass would have been gaming the measurement rather than fixing the failure.

| | v1 | v2 |
|---|---|---|
| Recall | 0.85 | **0.92** |
| Precision | 0.52 | **0.71** |
| Invented dates | 2 (fail) | **0** (pass) |
| UNSPECIFIED compliance | not measured | **2/2** |

---

## M4 — decisions

The capability test is not "find the decisions". It is that the
**proposed-then-deferred item is NOT recorded.**

So the prompt is organised around the negative case: the list of what *is not* a
decision comes before the rules, with deferral first and an instruction to read
it twice.

**The trap is real.** In the client status call a strong preference for one
analytics provider is voiced immediately before *"I think we should defer this
decision until we have the Q3 usage numbers in front of us"*. A model weighing
preference over the deferral records a decision that was never made, and the
reviewer cannot tell, **because the quote will be perfectly genuine.**

Six exclusions, each for a shape in the sample data: a deferral, a choice still
being argued, a preference, a commitment to do work, a statement of existing
fact, a recap.

**One positive rule that is easy to get backwards:** a deferral is sometimes
*itself* a decision, and a different one. The hotel kickoff postpones the
permanent product name and settles on `HotelOS` as a working name in the same
breath. **The working name IS a decision; the permanent name is not.**

`who_stated_it` feeds the cross-region deduplication rule — for a decision that
catches the same person restating what was settled earlier.

`stated_rationale` accepts `UNSPECIFIED`. An invented reason is the same failure
as an invented owner and is harder to spot, **because a decision log full of
tidy rationales reads better than one admitting nobody gave a reason.**

**Measured:** 3/3 golden decisions found, 0 deferred items recorded.

---

## M5 — risks and blockers

The capability test is unusual: severity must be **defensible from the quote
alone** — not correct in the abstract, but readable by somebody holding only the
quote.

So the prompt states that test back to the model, and the three bands are
defined by **what the speaker said would happen**, not by how serious the topic
sounds:

| Band | Requires |
|---|---|
| high | a stated serious consequence: a missed deadline, a delayed go-live, lost trust, a compliance problem, work that cannot proceed |
| medium | a real problem with a stated but non-severe impact, or a serious problem whose consequence is vague |
| low | a concern raised with no stated consequence |

*"Never use high because the topic sounds serious"* is in the prompt because
**every risk in this corpus concerns healthcare compliance or payments** and
would sound serious whatever was said. A model reaching for high on subject
matter alone would score correctly for the wrong reason.

Case `M5b` enforces it: every stored high-severity risk must quote a stated
consequence.

**Raising a risk is not owning it.** Somebody flagging a problem is not
automatically accountable, so `owner` is `UNSPECIFIED` unless the transcript
names one. The golden risk in the sprint planning transcript is exactly that
case, raised by Sarah Chen with nobody named.

Severity is a three-value enum rather than a score, because **a reviewer can
argue with "high" in a way they cannot argue with 0.72.**

**Measured:** 2/2 golden risks found, severity defensible 4/4.

---

## How it is tested

Every failure mode is tested by **scripting the failure**, because a live model
cannot be made to produce one on demand.

| Behaviour | How it is proved |
|---|---|
| A fabricated quote is repaired | Script one, assert the repair prompt names it and the corrected quote is stored |
| An unfixable quote is flagged, not discarded | Script a model that never corrects; assert the item is stored, flagged, sorted first, unapprovable |
| The deferred decision is absent | Assert absence — **and** script a model that records it, asserting it *does* then reach the store |
| Two commitments in one sentence stay separate | Same quote, different tasks, assert two survivors |
| A recap is merged | Same owner, same task, far apart, assert one survivor |
| Two risks by one person stay separate | Same owner, same description, assert both survive |
| Severity is defensible | Every stored `high` must quote a consequence word |
| Consent blocks before any call | Assert `provider.calls == []` |

**The golden-case-5 pair is the pattern worth noting.** A negative test that
cannot be made to fail proves nothing: asserting the deferred item is absent
would also pass if extraction silently did nothing. So a second test scripts a
model that records it and asserts it reaches the store.

---

## What it does not do

- **No confidence filtering.** Everything is stored and the queue sorts by
  confidence. The harness reports precision at five thresholds instead — and
  the finding is that **Gemini returns 0.95 for almost everything**, so its
  confidence is decoration. Prompt v2 says *"do not return 0.95 for
  everything"* and it made no difference. Recorded rather than hidden.
- **No cross-source merging.** An action in a meeting and the same action in a
  chat message are two records.
- **No re-extraction of approved items.** `delete_for_source` removes only
  pending rows: an approved or rejected item is a human decision and a re-run
  has no business discarding it.
