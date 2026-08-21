# 03 · Chunking and grounding

**Shared by:** M3, M4, M5
**Code:** `extraction/chunker.py`, `extraction/quote_verifier.py`, `extraction/dates.py`, `extraction/deduplicator.py`
**Tests:** 47 across `test_prompts_and_chunking.py` (14) and `test_quote_and_dates.py` (33)

> *"Chunking will decide your extraction quality. Document your chunking
> strategy; we will ask about it."* — the brief

---

## Chunking: four properties, four reasons

### 1. Chunks are built from whole segments, never split mid-segment

A segment is one person's turn. Splitting it separates a commitment from the
words that make it one, and produces a quote that cannot be a substring of any
single line.

### 2. Overlap is whole segments, not a character count

**A commitment is usually made across two turns:** somebody asks, somebody
agrees. If a boundary falls between them, one chunk sees a request with no
acceptance and the other an acceptance with no request — both wrong in the same
way. Repeating the last few whole turns means the pair appears complete in at
least one chunk.

The price is duplicates, which deduplication then removes. Paying that knowingly
beats missing commitments at boundaries.

### 3. Every chunk carries a non-quotable context header

```
CONTEXT (background only, never quote from this block)
  Meeting     : Sprint Planning - Meridian Platform
  Date        : 2024-11-18
  Participants: Sarah Chen, Priya Sharma, Priya Menon, James Liu, Marcus Webb, Tom Reynolds
  This chunk  : 1 of 3, covering 00:00:05 to 00:05:32
  Note        : the participant list is who was in the room. A person
                being present does not make them the owner of anything.
```

**Without the participant list the model cannot tell that "James" is James Liu,
or that two people called Priya are in the room.** Owner attribution is golden
case 3, and this is where its errors come from.

Marked non-quotable because a quote drawn from it would not be a substring of
the source and would fail verification.

### 4. Timestamps and speakers are rendered into the text

The model must return them, so it has to see them. An unlabelled speaker renders
as `UNSPECIFIED` — **the same token the model must output for an unknown
owner**, so the transcript and the prompt agree on what "not stated" looks like.

### Measured

Sprint planning: **55 segments → 3 chunks**, full coverage, 3-segment overlaps,
no segment split. Visible byte-for-byte at `GET /api/sources/{id}/chunks` and in
the interface's Pipeline tab.

Token counts estimate at **characters ÷ 4**, named as an approximation rather
than importing a tokeniser for a different model family. Chunks sit well below
any provider's context window, so the estimate has room to be wrong.

---

## Quote verification

> *"A substring check on every quote before a record is stored costs you five
> lines and eliminates the most damaging class of failure in this domain."*

The check is small. What makes it work is that it runs against **exactly the
string the ingestion pipeline built**, whitespace-normalised the same way, so a
quote wrapped across lines still matches and a quote the model invented cannot.

```python
normalise_text(quote) in source_text
```

Only whitespace is relaxed. Punctuation, casing and wording must match, because
a quote that has been tidied is no longer verbatim.

Verification also yields the **location** — `char_start`, `char_end`,
`segment_id` — so a citation points at a span inside the source rather than at
the source. The rubric's red flag is *"citations that point at a document but
not a location within it"*.

### The rejection message

Includes the longest prefix of the quote that **is** present, so the model can
see where it began to drift:

```
the quote 'I can have the refactor done by Monday' is not a literal substring
of the transcript. Only the first 34 characters match: 'I can have the refactor
done with'. The text diverges after that. Copy the words exactly.
```

A prefix shorter than **12 characters is not reported** — *"only the first 1
characters match"* is noise, and the quote is better described as absent. A test
caught that.

### An unverifiable quote is flagged, not discarded

The validator gets the full retry budget. If the model still cannot produce a
literal quote, the last schema-valid response is taken (served from the cache,
so it costs nothing) and the items are stored with `quote_verified = 0`, sorted
to the top of the queue, and **blocked from approval without an explicit
override and a written reason**.

**Why not discard:** it would make the fabricated-quote metric zero *by
construction*. The brief warns that a harness reporting everything passing
usually means the cases were too easy. The number has to be able to be non-zero
or it is not a measurement.

---

## Date discipline

> *"Assert that no action has a concrete due date unless the transcript states
> one or states a resolvable relative date. Where a relative date is resolved,
> the resolution rule must be documented."*

**Two rules govern everything:**

1. **Resolution is anchored to the meeting date, never to today.** Re-running
   the harness next month must produce the same answer.
2. **What no rule covers is not resolved.**

```
due_date          "2024-11-22"        what a consumer reads
due_date_type     relative_resolved   how it got there
due_date_stated   "Friday"            what the transcript said
due_date_rule     "friday = the first friday strictly after the meeting date 2024-11-18"
```

The three fields beside `due_date` are **not optional**: golden case 4 measures
invented dates, and a resolved date is otherwise indistinguishable from one the
transcript stated.

| Stated | Anchor 2024-11-18 | Type |
|---|---|---|
| `Friday` | 2024-11-22 | relative_resolved |
| `end of next week` | 2024-11-29 | relative_resolved |
| `tomorrow` | 2024-11-19 | relative_resolved |
| `in two weeks` | 2024-12-02 | relative_resolved |
| `December third` | 2024-12-03 | absolute |
| `early October` | **UNSPECIFIED** | not resolved |
| `soon`, `next sprint`, `after the audit` | **UNSPECIFIED** | not resolved |

An approximate date presented as a real one is precisely the invented-date
failure golden case 4 probes for.

A named calendar date with no year takes the **first occurrence on or after** the
meeting date — a due date that has already passed is never the intended reading.

---

## Deduplication

Duplication has **two different causes**, so there are two rules.

### Rule one — within a region

Chunk overlap causes the same words to be read twice. **Both signals required:**

- quotes identical after normalisation, **or** quote spans overlapping by ≥ 50%
  of the shorter span
- **and** ≥ 40% of the shorter task description's content words appear in the
  longer one

**Region alone is not enough:** *"I'll write the tests and Priya will review the
schema"* carries two commitments, and a model extracting each quotes the same
sentence for both.

**Task alone is not enough:** two people can commit to similar work in different
parts of a meeting.

**Containment rather than Jaccard**, because the model describes the same
commitment at different lengths in different chunks and Jaccard penalises that:

```
"Finish auth refactor with integration tests"
"Complete the authentication refactor and its tests"
     Jaccard 0.29  ·  containment 0.50  ·  plainly the same commitment
```

### Rule two — across the meeting

A meeting that ends *"so to recap, Priya is finishing the auth refactor, James
is setting up the pipeline"* restates every commitment **thousands of characters
from where it was made**. Span comparison cannot see that far.

Caught instead by **the same named owner and ≥ 70% task containment, with no
span requirement.** Higher threshold because there is no span evidence, only the
wording.

Measured on the sample data: 0.7 merges every recap restatement and merges
nothing it should not — the nearest non-duplicate pair by the same owner scores
0.0. `UNSPECIFIED` owners are excluded, since "the same owner" is meaningless
when nobody was named.

**This rule is switched off for risks.** The person named on two risks is
usually the person who *noticed* both, not the person recommitting to one.
Merging would silently discard a real concern, and a lost risk is worse than a
duplicate one a reviewer dismisses in a click.

### Two principles

**When the rules disagree, both candidates are kept.** A duplicate in the queue
is visible and dismissed in one click. A wrong merge is invisible and has
destroyed a real commitment.

**The survivor is kept whole.** Fields are never mixed between two candidates,
because a record assembled from two model outputs is one no model produced, and
neither its quote nor its owner could then be traced to a single place.
Confidence ties break toward the earlier quote, so a recap never wins over the
moment the commitment was made.

---

## What it does not do

- **No semantic chunking.** Embedding-based topic detection would need the model
  loaded during extraction, is slower, and is hard to defend as necessary on a
  12-minute transcript.
- **No embedding-based deduplication.** The threshold would be arbitrary,
  results would shift with the embedding model, and it is harder to defend than
  a rule you can state in a sentence.
- **Relative dates with no anchor stay UNSPECIFIED.** A source with no meeting
  date cannot resolve anything, and the rule records that as the reason.
