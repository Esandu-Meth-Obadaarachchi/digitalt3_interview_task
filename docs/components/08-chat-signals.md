# 08 · Chat signals

**Capability:** M9
**Code:** `ingestion/chat_export.py`, `extraction/signals.py`, `db/repositories/chat.py`
**Tests:** 17 in `test_chat_signals.py`, plus harness cases 7, 7b, 7c

---

## Measured

| Metric | Result | Target |
|---|---|---|
| Chat signal precision | **0.87** | ≥ 0.70 |
| **Direct messages in the store** | **0** | 0 |

78 messages classified in 5 batches, 48 discarded as noise, 21 queued for review.

### Per class, because the headline hides the shape of the errors

| Class | Precision | Recall |
|---|---|---|
| decision | 1.00 | 1.00 |
| question | 1.00 | 1.00 |
| blocker | 1.00 | 0.75 |
| request | 0.60 | 0.75 |
| noise | 0.60 | 0.60 |

**The entire confusion is between noise and request, and nothing else.** That is
more useful than 0.87, and it points at where a prompt revision would pay.

A request is the hardest of the five to define: *"can somebody look at this"* and
*"someone should look at this"* differ only in **whether anybody was actually
asked.**

---

## Direct messages, excluded three ways

This is the one property here that **cannot be walked back if it fails.** A
private conversation that reaches the store has already reached it.

| # | Mechanism |
|---|---|
| 1 | The parser drops them and never returns them |
| 2 | `CHECK (is_direct_message = 0)` — one cannot be stored even by a direct INSERT |
| 3 | The golden case checks the 12 forbidden ids against what actually reached the store |

**A DM is identified two ways, and either is enough:** the export's own flag, or
a channel name that looks like a direct thread. Trusting only the flag would
mean an export that omits it **silently leaks private conversation.**

### The count is the only evidence

The messages leave no trace by design, so `direct_messages_excluded: 12` on the
ingestion report is the **only** proof they were ever seen. Without it, "zero DM
records in the store" is indistinguishable from "the export contained no DMs".

The interface shows that count beside the channel list rather than burying it in
a report.

### A dishonest zero is guarded against

Zero direct messages is **trivially true of an empty store.** Case 7b therefore
also requires that messages *were* stored, and fails with *"nothing is stored,
so zero DMs proves nothing"* otherwise.

---

## Why chat does not use the transcript pipeline

A transcript is a **continuous conversation** where meaning spans turns, so it is
chunked with overlap and a context header.

A channel is a **list of discrete messages**, each classified on its own, with an
id that must come back attached to the right one.

Forcing one shape onto both would have made both worse.

**Batched per channel, in timestamp order, twenty at a time.** Order matters even
though each message is labelled separately, because *"can you take a look?"* is
unreadable without the message before it. Twenty at a time because 78 messages
must not cost 78 requests against a daily allowance of 20.

---

## Three checks inside the retry loop

| Check | Why |
|---|---|
| Every `message_id` was in the batch | A label on the wrong message is **worse than no label**, and the mistake is invisible |
| Every message sent came back | A missing entry is **not the same** as one labelled noise, and the difference changes precision |
| Every quote is a substring of **that message** | Not of the channel — a quote from a different message would verify against the corpus and mislabel this one |

Both failure modes are tested by **scripting them**: a model that drops three
messages from a batch, and a model that invents a message id. Each is asserted
to trigger a repair.

---

## After classification

| Class | Stored? | Queued? | Why |
|---|---|---|---|
| decision | yes | **yes** | could produce a downstream write |
| blocker | yes | **yes** | could produce a downstream write |
| request | yes | **yes** | could produce a downstream write |
| question | yes | no | answering one writes nothing anywhere |
| noise | **deleted** | no | the brief says discarded, not stored |

**Noise is deleted, not stored with a label.** The schema enforces the same rule
from the other side: `noise` is absent from the classification CHECK and could
not be written even deliberately.

Queueing a question would put work in front of a reviewer that has no downstream
effect.

---

## The prompt is built around choosing noise

M9 is measured on **precision**. That decides the shape: the instruction to
choose noise gets its own section, and the reason is stated rather than asserted.

**Every non-noise label becomes something a human has to review.** So labelling a
greeting as a request costs more than labelling a borderline request as noise. A
channel's real signal is thin — five of the twenty hand-labelled messages are
noise.

Each of the four real labels carries the line that separates it from the nearest
thing it is not:

| | Not |
|---|---|
| decision | a proposal, a preference, something still being argued |
| blocker | slow or annoying. **The work must actually be stopped** |
| question | rhetorical, thinking aloud |
| request | a general wish. A named or clearly implied person is being asked |

*"Do not reach for a label because a message mentions work"* is in the prompt
because it is the failure mode this data produces. *"The auth refactor is going
well"* is narration; *"the auth refactor is blocked on the Redis upgrade"* is a
blocker. **The difference is the consequence, not the subject.**

---

## A scoring trap that had to be got right

**A message absent from the store counts as a prediction of noise** — because
that is what its absence means.

Getting this wrong would have **quietly inflated precision** by dropping every
noise prediction from the denominator.

---

## What it does not do

- **No thread reconstruction.** `thread_id` is stored and not used. Threading
  would change the batching, and nothing in the golden set depends on it.
- **No cross-channel deduplication.** The same request in two channels is two
  signals.
- **No author identity resolution.** "Priya Sharma" in a channel and "Priya" in
  a transcript are not merged. A stretch item, deliberately not attempted.
