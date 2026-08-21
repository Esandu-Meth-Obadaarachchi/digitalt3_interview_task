# 10 · Outcome records

**Capability:** M11
**Code:** `models/outcome.py`, `outcome/record.py`, `adapters/store.py`, `adapters/mock_store.py`
**Tests:** 13 in `test_outcome_records.py`
**Schema:** [`../outcome_schema.json`](../outcome_schema.json)

---

## The acceptance test shaped the design

> *"A second process can read a record and reconstruct the approved items
> without any access to the transcript store."*

So **everything a consumer needs is inside the file.** Nothing is a foreign key
into a database they do not have.

Each item carries its **own** quote, speaker, timestamp and source id rather than
an extraction id to look up. The record is larger for it, **and that is the
point:** a record that requires this application to interpret is not a record a
delivery agent can consume.

`test_a_second_process_can_reconstruct_the_items_with_no_database` does a
`json.load` and nothing else. No database connection, no application code beyond
the standard library:

```
schema_version 1.0 | consent_flag True | 5 signals
  - [blocker] Heads up: the staging environment is down. The SSL certificate...
      #proj-meridian-dev · Marcus Webb · approved by esandu
      quote verified: True
```

---

## What travels with each item, and why

| Field | Why it is in the file |
|---|---|
| `citation.quote` + `quote_verified` | A consumer can check the claim, and knows whether the check was machine-run |
| `approved_by` / `approved_at` | **The entire value of this record is that a human accepted every line.** An agent acting on one should be able to say who accepted it |
| `edited_by_reviewer` | A consumer should know a person changed it from what the model proposed |
| `consent_flag` | **Consent travels with the content.** A downstream agent has no other way to know whether the meeting permitted processing at all |

### The excluded counts

```json
"pending_not_included": 15,
"rejected_not_included": 1,
"expired_not_included": 0
```

Without these, **an empty record is ambiguous** between *"nothing was found"* and
*"nothing has been reviewed yet"* — and those call for opposite responses from
whatever reads it.

---

## Approved items only

Not a filter applied politely at the end. An outcome record is **the artefact a
downstream agent acts on**, so including a pending item would route around the
approval gate the whole system exists to enforce — at the last possible moment
and in the least visible place.

**A non-consented source gets no record at all.** An empty one would imply the
source was handled; refusing says it was not.

---

## Versioned, never overwritten

Emitting twice produces `v001.json` and `v002.json` side by side.

**A consumer that read version 1 and acted on it should be able to see what it
read.**

The version is in the filename *and* inside the document, so the store can be
listed and understood without opening anything.

```
data/documents/outcome_records/chat-export-meridian-2024-11-20/v001.json
data/documents/outcome_records/chat-export-meridian-2024-11-20/v002.json
```

---

## Written through the store adapter

This module **never learns what a filesystem is.** A real document platform is
one class away.

`test_a_record_is_read_back_through_the_store_not_the_database` overwrites the
document with a different `schema_version` and asserts `load_record` returns the
new one. **If it read the database copy, the test fails** — the claim is that a
consumer needs no database, and reading it back any other way would not test
that.

### The store refuses an escaping key

Keys are derived from source ids, **which arrive over HTTP**. A key of
`../../etc/thing` must not be writable, and the check belongs in the store
rather than in every caller.

---

## The published schema

[`docs/outcome_schema.json`](../outcome_schema.json) is **generated** from the
Pydantic contract by `make outcome-schema`.

**A hand-written schema drifts from what is actually emitted, and a consumer
trusting the drifted version is worse off than one with no schema at all.**
`test_the_published_schema_matches_the_contract` compares them.

The **consumer contract** at the top is hand-written, because it says what the
fields *mean* and JSON Schema cannot carry that:

```
Check schema_version before reading. This document describes 1.0.
Every item in actions, decisions, risks and signals was approved by a named human.
consent_flag is the consent state of the meeting these items came from.
  Do not act on items from a record whose consent_flag is false; none should exist.
citation.quote is a literal substring of the source transcript.
  citation.quote_verified says whether that was machine-checked.
pending_not_included, rejected_not_included and expired_not_included let you tell
  an empty record that means 'nothing was found' from one that means
  'nothing has been reviewed yet'.
```

`schema_version` is a **field**, not a filename convention, because consumers are
expected to check it and a filename is not somewhere a program looks.

---

## What it does not do

- **No incremental records.** Each version is complete. Diffing two versions is
  the consumer's job, and a delta format would need a merge rule this build has
  no caller for.
- **No signing or checksums.** A real handoff between agents would want them.
- **No retraction.** An approved item that later proves wrong stays in the
  emitted record; a new version reflects the change, and the old one still says
  what it said.
