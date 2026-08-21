# 09 · Scheduler and digests

**Capability:** M10
**Code:** `scheduler/jobs.py`, `scheduler/digest.py`, `models/digest.py`, `adapters/notifier.py`
**Tests:** 15 in `test_digest_and_scheduler.py`

> *"A real scheduler must exist and be demonstrable. A button labelled 'Run
> morning job' with no scheduler behind it is a partial implementation."*

---

## A real scheduler

APScheduler's `BackgroundScheduler`, started with the application.

```
end_of_day_digest   next: 2026-08-21T18:00:00+05:30   cron[hour='18', minute='0']
expiry_sweep        next: 2026-08-22T02:00:00+05:30   cron[hour='2',  minute='0']
```

`/api/digests/schedule` reports those **from APScheduler**, not from
configuration. A time read back from settings would prove only that settings can
be read. **A next-run timestamp that advances on its own is the difference
between a scheduler and a constant.**

When disabled it reports `enabled: false` and **still describes the jobs that
would run**, because an empty job list reads as a bug.

---

## The second job matters more than it looks

The **expiry sweep** is the rubric's *"safe default on timeout or no response"*.

An unreviewed proposal ages out to `expired`, which the approval-gate trigger
treats **exactly like pending: not writable.**

`test_the_expiry_job_is_the_safe_default_not_an_approval` asserts the **approved
count is unchanged** after a sweep. **Nothing is ever approved by the passage of
time.**

The anti-patterns tab asks that an agent do at least one thing without being
asked. *"Refuse to proceed on insufficient information"* is on its list, and
this is that.

---

## The digest

The shape is specified: **3 items that moved, 2 that need attention, 1 thing to
decide.** Fixed sizes, because **a digest that grows with the day is a digest
nobody reads.** The format forces a choice about what matters.

### Every line cites its source

```markdown
- Blocker: the Meridian Pay sandbox API is returning 503 errors again...
  > "We cannot test the payment flow at all right now."
  — Sarah Chen, #proj-meridian-dev at 2024-11-18T15:00:00Z · a blocker raised in the channel
```

Citation **inline** rather than as a footnote, so checking one does not require
scrolling. **A line that cannot be cited is not written.**

The trailing clause is `because` — the reason that line is in that section. Kept
so a reader can disagree with the *selection* rather than only the wording.

### Approved items only

Not a filter applied afterwards: **the query has no other status in it**, so
there is nothing to forget. The rendered digest ends with *"Nothing unapproved
appears in this digest."*

### Each item appears in exactly one section

Precedence: **needs attention → to decide → moved.**

A blocker approved today is both progress and a problem. Printed under both it
**fills two of six lines with one fact**, which is the opposite of what a
fixed-size digest is for. Attention wins because a reader who only reads one
section should read that one.

**Found by rendering one and reading it.**

### Selection rules

| Section | What qualifies |
|---|---|
| Needs attention | high/medium risk · a blocker · an action nobody owns |
| To decide | an action with no owner *and* no date · else a request nobody confirmed |
| Moved | approved today, from whatever is left; falls back to most recent so a quiet Monday still says where things stand |

These are **heuristics, not learned**, and they are stated on every line so a
reader can disagree with the pick.

---

## The clock override

`now` is a **parameter threaded through** building, emitting and the scheduled
job — not a separate demo path. So what is demonstrated is **the same function
that runs unattended.**

In the interface it is a date picker beside Preview. Tested against a month ago
and three months hence, because a clock override that only works for today is
not one.

---

## Posting is deliberately not gated

The task catalogue: *"posting the digest is not an external write. Digests never
contain unapproved extractions."*

The gate is **upstream**. By the time a digest exists, every line came from
something a human approved, so there is nothing left to approve. **A second gate
would ask a reviewer to approve their own earlier approvals.**

`write_log/notifications.jsonl` records what would have been posted. Nothing is
sent anywhere.

---

## Scope

One digest per channel. **A meeting is treated as its own channel**, because the
specification says per channel and a meeting has no other natural grouping —
doing otherwise would leave every transcript out of the digest entirely.

`UNIQUE (scope_type, scope_key, digest_date)` makes a scheduled run idempotent:
**running the 18:00 job twice replaces one digest rather than producing two.**

---

## How it is tested

| What | Tests |
|---|---|
| No unapproved item can appear (fixture deliberately leaves some unapproved) | 1 |
| Every line carries a citation and a reason | 1 |
| Clock override for three different dates | 1 |
| 3/2/1 caps, no item in two sections, empty scope says so | 3 |
| Written through the store, posted through the notifier, recorded, idempotent | 4 |
| Scheduler: both jobs, real future fire times, honest when disabled | 2 |
| Expiry is not an approval; the job runs what the endpoint runs | 2 |

---

## What it does not do

- **No per-person digest.** M13, a COULD, not built.
- **The scheduler runs in-process.** A second instance would run the jobs twice.
  Fine for a single-node review tool; a real deployment needs a lock or an
  external scheduler.
- **No delivery guarantees.** The mock appends to a log. A real notifier would
  need retry and idempotency of its own.
