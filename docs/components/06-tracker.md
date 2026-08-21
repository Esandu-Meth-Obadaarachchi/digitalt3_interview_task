# 06 · The tracker adapter

**Capability:** M7
**Code:** `adapters/tracker.py`, `adapters/mock_tracker.py`, `tracker/service.py`, `tracker/write_log.py`
**Tests:** 40 across `test_tracker_adapter.py` (21) and `eval/test_approval_gate.py` (19)

---

## The capability test, executed

> *"Approve three actions, then re-run the write path twice: exactly three
> tracker items exist. The write log shows every attempt including the
> deduplicated ones."*

```
approve 3 → sync → sync
  3 created · 6 deduplicated · 2 blocked
  15 tracker items: 3 written by the agent, 12 pre-existing
  write log: 11 attempts
```

It is a **test**, not a demo step:
`test_the_capability_test_three_approvals_two_reruns_three_items`.

---

## The interface

```python
class TrackerAdapter(ABC):
    def create_item(draft) -> TrackerItem
    def get_item(external_ref) -> TrackerItem | None
    def list_items(criteria) -> list[TrackerItem]
    def transition(external_ref, status) -> TrackerItem
```

**Four operations, every one with a caller.** `add_comment` appears in the
brief's illustrative example and is deliberately absent, because nothing in this
build comments on a ticket. A test asserts the abstract method set, so widening
it silently is not possible.

**Nothing in the interface knows what a tracker is made of.** No SQLite, no
JSONL, no Jira, no extraction. The one link back is `source_ref`, which a real
integration would put in a custom field or the description.

**The gate is deliberately not in the adapter.** It sits in the service above
and in a trigger below, because putting it in the adapter would mean every
future implementation had to reimplement it — and the one that forgot would be
the one that mattered.

---

## Three tables, three questions

| Table | Answers |
|---|---|
| `tracker_items` | What the tracker holds, including the seeded backlog |
| `tracker_writes` | Our audit of what we put there. `UNIQUE (extraction_id)` |
| `tracker_write_attempts` | What the agent *tried*, including refused ones |

Conflating the first two would mean the agent could not distinguish its own
writes from somebody else's tickets — exactly the position a real integration is
in.

---

## Realistically messy mock data

> *"Mocks must return realistically messy data... An agent that only works on
> clean data has not been tested."*

12 seeded tickets the agent never created:

| | |
|---|---|
| No assignee | 3 |
| No due date | 6 |
| Due date already in the past | 4 |
| No labels | 2 |
| Distinct free-text statuses | **9** |

Including `"In Progress "` with trailing whitespace **beside** `"In Progress"`,
`"blocked - waiting on IT"` as a sentence, `"Done."` with a full stop, and a
pair of near-duplicate tickets for the same bug raised twice.

Status filtering compares the trimmed, lowered form — that mess is normal, and
the tolerance belongs in the query rather than in the data.

### A test caught the contract sanitising it

`TrackerItem` was silently stripping whitespace, because `StrictModel` sets
`str_strip_whitespace`. That quietly normalised `"In Progress "` into
`"In Progress"` and **hid exactly the mess the contract requires the agent to
cope with.**

`TrackerItem` now sets `str_strip_whitespace=False`: **our own contracts
normalise, foreign data is preserved as found.**

---

## A design smell a test caught

`test_nothing_above_the_interface_imports_the_mock` asserts that only
`factory.py` names a concrete adapter.

An earlier version had `tracker/service.py` importing `MockTracker` to append to
its write log — **the mock's shape leaking into agent logic**, the specific
thing the adapter contract penalises.

Recording what the agent attempted is the *agent's* audit, not tracker
behaviour. It moved to `tracker/write_log.py` and now works identically
whichever adapter is configured, so a real integration inherits it for free.

---

## What a written ticket carries

M7 requires the source ID, timestamp and quote in the description, because **a
ticket saying only "finish the auth refactor" is a ticket nobody can check.**

```
Finish the authentication module refactor with integration tests

Quoted from the meeting: "I can have the refactor done with tests by Friday"
Said by: Priya Sharma
At: 00:02:17
Source: meeting-sprint-planning-2024-11-18
Extraction: meeting-sprint-planning-2024-11-18::action::6d4cd399
Stated timing: "Friday" -> 2024-11-22 (friday = the first friday strictly after...)
```

| Situation | Result |
|---|---|
| Owner `UNSPECIFIED` | **unassigned**, labelled `needs-owner`, never a guess |
| Date `UNSPECIFIED` | no due date, labelled `needs-date` |
| Quote unverified (overridden) | `WARNING` in the description, labelled `unverified-quote` |

A ticket assigned to nobody is a **correct record of a commitment nobody
claimed.** Inventing an assignee at the write stage would undo the discipline the
extraction stage maintained.

---

## Approval writes through, and a failed write does not undo it

The task catalogue gives M7 the trigger *"on approval"*, so approving writes
immediately. The write happens **after** the approval transaction commits.

If it fails, the human decision stands and `sync_approved` retries. **An
approval that silently reverted because a downstream system was unreachable
would be worse than one that is merely not yet written.**

### A compensating delete, not a distributed transaction

The adapter and our audit sit either side of a system boundary and cannot share
a transaction. If the audit insert fails after the item was created, the item is
deleted and the error re-raised. **A real integration has the same problem and
the same answer.**

---

## The write log

`write_log/tracker_writes.jsonl` — one line per attempt, including deduplicated
and blocked:

```json
{"attempted_at":"...","extraction_id":"...","outcome":"blocked","provider":"mock",
 "extraction_status":"pending","reason":"...only an approved extraction may be written"}
```

JSONL rather than a table, because during a walkthrough **a log can be read
aloud and a table has to be queried.** It belongs to the agent, not the tracker,
so a real integration inherits it.

**A log that recorded only successes could not prove a gate ever fired.**

---

## What it does not do

- **No endpoint writes an arbitrary payload.** The only route in is an approved
  extraction. An endpoint accepting a free-form item would be the exact hole the
  rubric describes, however convenient for testing.
- **No real integration.** The contract states one earns no extra marks; a
  clean swappable mock earns full marks.
- **No retraction.** Nothing here deletes a tracker item.
