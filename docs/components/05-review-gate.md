# 05 · The review queue

**Capability:** M6
**Code:** `review/queue.py`, `db/repositories/reviews.py`, plus 8 database triggers
**Tests:** 39 across `test_review_queue.py` (20) and `eval/test_approval_gate.py` (19)

> *"Nothing gets written to any external system without explicit human
> approval. This is tested and is an automatic failure if violated."*

This is the property the whole build is organised around.

---

## Four states, one of them writable

```
                    ┌─────────────┐
                    │   PENDING   │ ◄── every extraction starts here
                    └──────┬──────┘
            ┌──────────────┼──────────────┐
       approve           reject        expire
            │              │              │
            ▼              ▼              ▼
      ┌──────────┐  ┌──────────┐  ┌──────────┐
      │ APPROVED │  │ REJECTED │  │ EXPIRED  │
      │ writable │  │          │  │          │
      └──────────┘  └──────────┘  └──────────┘
            └──────────────┴──────────────┘
                    all terminal
```

**`approved` is the only writable state.** `rejected` and `expired` are treated
identically by the approval gate: not writable.

**Terminal means terminal.** `trg_extractions_terminal_status` refuses any
change once set, closing the route where a rejected payload is reopened and
pushed through.

**`expired` is the rubric's "safe default on timeout or no response".** A
pending item older than `PENDING_EXPIRY_HOURS` is swept by a scheduled job.
**Nothing is ever approved by the passage of time** — a test asserts the
approved count is unchanged after a sweep.

---

## Enforced at four depths

| # | Mechanism | Survives |
|---|---|---|
| 1 | `review/queue.py` refuses and explains | ordinary use |
| 2 | `tracker/service.py` refuses before a draft is built | the queue being bypassed |
| 3 | `trg_approval_gate_write` | the service being bypassed |
| 4 | `UNIQUE (tracker_writes.extraction_id)` | a forged duplicate |

The rubric's red flag is gating that *"exists in the UI but is bypassable via
the API"*. So golden case 8 **never goes through HTTP.** It calls the service
directly, then goes lower still:

```python
conn = sqlite3.connect(settings.db_path)          # no Python service in the path
with pytest.raises(sqlite3.IntegrityError, match="approval_gate"):
    conn.execute("INSERT INTO tracker_writes ...", (pending_id, ...))
```

Tested for `pending`, `rejected` **and** `expired`.

**The obvious bypass is also closed.** Flipping the status by hand fails:
`trg_extractions_review_audit` refuses an approval that names nobody and gives
no time.

---

## Rules that live above the database

Three things the schema cannot express:

### 1. An unverified quote needs an override *and* a written reason

The database would accept the row. **A distracted click should not.** The reason
is recorded in the audit trail prefixed `OVERRIDE of unverified quote:`, so a
later reader can tell a considered acceptance from an ordinary approval.

### 2. Every transition writes an append-only audit event

`review_events` records `created`, `edited`, `approved`, `rejected`, `expired`
with the actor, the time, and the payload **before and after**. Two triggers
refuse any UPDATE or DELETE.

*"Who approved what, when, and what did they change"* is answerable from this
table alone.

### 3. An edit is not an approval

Editing leaves the item `pending`. The corrected item must still be approved.

**`original_payload` is immutable**, enforced by trigger, so the review surface
can always show what the model said beside what a human changed it to. The model's
first output is evidence.

---

## What the reviewer sees

For every item: the **verbatim quote** in monospace with a verification badge and
the character offsets into the source; the model's output beside any human edit,
struck through; the append-only audit trail; and whether the source stated an
owner and a date or the system abstained.

`UNSPECIFIED` renders in amber with a tooltip saying the source did not state
it — **never as an empty cell.** Abstention is an answer, not missing data.

Unverified quotes **sort to the top of the queue**. They are the ones a reviewer
must look at.

---

## Signals go through the same gate

Chat messages classified as `decision`, `blocker` or `request` become pending
extractions, because each could produce a downstream write.

A `question` is classified and kept but **not queued** — answering one writes
nothing anywhere, so queueing it would put work in front of a reviewer with no
downstream effect.

---

## How it is tested

| What | Tests |
|---|---|
| Queue state, sorting, summary | 3 |
| Editing: stays pending, original survives, change recorded | 3 |
| Approve and reject: reviewer recorded, terminal states, clean errors | 6 |
| The unverified-quote override: refused plainly, refused without a reason, allowed and marked | 3 |
| Expiry: not an approval, unapprovable after, attributed to `system`, leaves fresh items alone | 4 |
| Audit trail: who did what, and immutability | 2 |
| **Golden case 8 — approval enforcement at every depth** | **19** |

Golden case 8 also records what it **cannot** prevent:
`test_the_adapter_cannot_be_used_to_smuggle_an_unapproved_item` documents that
calling the adapter directly creates an item with no audit row. The item exists
and has no `tracker_writes` row, so the accounting shows it for what it is.
Recorded as a boundary rather than claimed impossible.

---

## What it does not do

- **No authentication.** The reviewer is a supplied name. Explicitly out of
  scope in the brief; a real deployment takes it from the session.
- **No bulk approve.** Deliberate: approving twenty items in one click is
  approving nothing.
- **No un-reject.** Terminal is terminal. Re-extracting produces a new pending
  item with a new id, which is the honest way to reconsider.
