# 13 · Per-person digests

**Capability:** M13 (COULD)
**Code:** `backend/app/people/identity.py`, `backend/app/scheduler/person_digest.py`
**Tests:** 44 — `test_person_identity.py` (25), `test_person_digest.py` (19)

```
approved actions (every source)
        │
        ▼
  group_owners()  ──────────►  people, unassigned last
        │                       key · display_name · aliases · ambiguous
        ▼
  build_person_digest(key)  ─►  every commitment, quoted, dated or UNSPECIFIED
        │
        ├── empty?  ──────────►  no digest. Nothing written anywhere
        ▼
  emit_person_digest()  ─────►  document store + digests table (scope_type 'person')
```

---

## What the capability asks for

> Per-person view of their commitments. Person with no commitments gets no digest.

Two sentences, and the second is the harder one. It is a statement about
absence, so it is enforced where digests are emitted rather than by rendering
an empty file that nobody reads to discover it is empty.

---

## Decisions

### It reuses the digest machinery and none of its shape

| | Channel digest (M10) | Person digest (M13) |
|---|---|---|
| Scope | one source or one channel | **every source** |
| Size | 3 moved / 2 attention / 1 to decide | **uncapped** |
| Empty scope | writes "nothing approved for this day" | **writes nothing at all** |
| Posted | yes, through the notifier | **no, by default** |

The 3/2/1 shape exists to force a choice about what matters across a whole
channel. Capping somebody's own commitments would silently drop the fourth one,
which is the opposite of what a per-person view is for.

It is cross-source because a commitment is a commitment whichever meeting it
was made in. A channel is a place; a person is not.

Person digests are **written and not posted**. A channel digest belongs in its
channel. One person's workload posted into a shared channel is a different
thing from the digest they asked for. `POST_PERSON_DIGESTS=true` turns it on for
a demonstration, and a test asserts the default is off.

The citation and approval-date helpers are the channel digest's, imported
unchanged. **A person digest cites exactly what a channel digest cites**, and
two implementations of one citation would be two things to keep honest.

### Two people sharing a first name are one person

The owner of an action is free text lifted from a transcript. One person is
"Priya" in one line, "Priya Sharma" in another and "priya" in a third. Grouping
on the raw string produces three digests for one person, which is not a
per-person view of anything. So names group on their **first name, casefolded**.

The consequence is that **Priya Sharma and Priya Menon share a digest**, and the
sprint planning transcript contains exactly that pair on purpose.

That was the instruction, and it is also defensible, but only because the cost
is paid back in evidence rather than hidden:

- every line carries `owner_as_stated`, the string exactly as the transcript
  gave it
- a grouped person reports `ambiguous`, and the digest text says *"Grouped by
  first name, so this covers Priya Menon, Priya Sharma"*
- the interface badges the person and lists the aliases under their name

The alternative, splitting on the full name, is worse in the common case: the
transcript usually says "Priya", so the split produces one digest for a first
name and another for a full name and the person reads neither in full.
**Grouping and showing the evidence beats splitting and showing neither.**

`PERSON_IDENTITY=full_name` switches to the strict rule. Both are tested.

### A bare first name takes its full name from the participant list

"Priya" alone becomes "Priya Sharma" when the meeting's own metadata names
exactly one participant with that first name. That is **a lookup in supplied
metadata, not a guess**. With two candidates it declines and keeps the bare
first name, which is the same abstention discipline the extraction uses.

Titles are dropped first, without which "Dr Priya" becomes a person called Dr.

### Unowned work gets its own digest, and says so

Dropping it hides real work. Assigning it invents an owner. So everything with
`owner = UNSPECIFIED` collapses into one bucket keyed `unassigned`, headed
**Assignee unspecified**, where every line states the task and then says the
assignee is unspecified:

```markdown
# Assignee unspecified — 2026-08-22

2 approved commitment(s) with nobody named.

_Every line states the task, and the assignee is unspecified. Nothing here was
assigned by guessing._

- Check the migration path for the legacy data
  due UNSPECIFIED, no date was stated · assignee UNSPECIFIED, nobody was named
  > "Someone needs to check the migration path for the legacy data"
  — Sarah Chen, Sprint Planning - Meridian Platform at 00:05:40
```

`UNSPECIFIED`, blank, `TBD` and `unknown` all collapse to the same bucket, and
**no placeholder is ever promoted into a display name**. A test asserts no
unowned item ever appears in a named person's digest.

### Dates are reported, never resolved into a guess

A commitment with no stated date prints *"due UNSPECIFIED, no date was stated"*.
The person digest is a reading surface, so it inherits the extraction's
abstention rather than adding a deadline nobody set.

---

## How it is tested

The approval gate is re-tested here rather than assumed. A person digest is a
new path to approved data, and every new path is a new chance to leak an
unapproved item:

- the union of every line across every person is a subset of the approved set,
  with the fixture asserting something was deliberately left pending
- every line carries a quote and a source id

The rules the capability states outright:

- a person with no commitments gets `None`, and **both the document store and
  the `digests` table are checked to be empty** afterwards
- `emit_all_people` writes one per person and never an empty one
- the API returns 404 for both the preview and the write

The grouping, and the evidence that survives it:

- two people sharing a first name produce **one** key
- a grouped digest names the full names it covers
- every line's `owner_as_stated` is one of the person's aliases

Plus the clock override, the stored `scope_type`, and that nothing is posted by
default.

One test is about the fixture rather than the code: it asserts the sprint
transcript still plants commitments nobody owns. **Half of what M13 asks for is
untestable if the sample data stops containing unowned work.**

---

## What it does not do

- **No nicknames, initials or email matching.** Bob for Robert needs a source
  of truth this build does not have. `L31`.
- **No per-person delivery.** The digest is written to the document store. Who
  it reaches is the notifier's problem, and posting is deliberately off.
- **Actions only.** Decisions and risks are not commitments, and putting them
  in a personal digest would make it a second channel digest with a name on it.
