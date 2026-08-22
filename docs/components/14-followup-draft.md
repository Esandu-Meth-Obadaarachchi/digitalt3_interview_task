# 14 · The follow-up draft

**Capability:** M12 (COULD)
**Code:** `backend/app/followup/draft.py`, `backend/app/routers/followups.py`
**Tests:** 32 — `test_followup_draft.py`

```
approved extractions for one source
        │
        ▼
   build_draft()   ─► subject + markdown, every line quoted   (nothing stored)
        │
        ▼
   create_draft()  ─► followup_drafts v1, v2, …  status 'draft'
        │
        ▼
   edit_draft()    ─► edited_body beside generated_body       status 'edited'
        │
        ▼
   send_draft(sent_by=a person)  ─► notifier                  status 'sent'
        │
        └── blank name, or a service name?  ─► 403, three times over
```

---

## What the capability asks for

> Generate a recap email/message from approved items. Human edits and sends.
> Agent never sends.

Every clause is about **who acts**, so the module is organised around that
rather than around text generation.

---

## Decisions

### The recap is rendered, not written by the model

This is the decision worth defending, and the reason is the one the whole build
turns on. **A model asked to summarise approved items produces sentences nobody
approved.** The reviewer approved a task description and a quote. A paraphrase
of five of those, however good, is new text that passed no gate.

A template produces exactly the approved text with its quote attached:

```markdown
## What people committed to

- Finish the authentication module refactor with integration tests
  Priya Sharma · due 2024-11-22
  > "I can have the refactor done with tests by Friday"
  — Priya Sharma at 00:02:17
- Check the migration path for the legacy data
  assignee UNSPECIFIED, nobody was named · no date stated
  > "Someone needs to check the migration path for the legacy data"
  — Sarah Chen at 00:05:40
```

Plain, cited, and dull on purpose. A recap is one of the few documents where
saying it plainly beats saying it well, and it costs **no model call**, which
matters against a free tier of twenty requests a day.

The person still gets to write. They edit the draft, and what they send is what
they wrote.

### Drafting is not gated. Sending is

Drafting is not an external write: nothing leaves the machine, and every line
came from an extraction a human already approved. A second gate here would ask
a reviewer to approve their own earlier approvals — the same argument the
digest makes.

Sending is an external write, and it is **the only thing in this build a person
triggers by hand every single time**. There is no scheduled counterpart.

### `sent_by` is the whole capability

It is not a courtesy field. It is the difference between a person sending a
message and an agent sending one, so it is refused four ways:

| Depth | Mechanism | Refuses |
|---|---|---|
| 1 | `send_draft()` | blank name → `AgentSendRefused`, 403 |
| 2 | `send_draft()` | `agent`, `system`, `scheduler`, `bot`, `service`, `llm`, `model` |
| 3 | `trg_followup_send_requires_person` | any UPDATE to `sent` with no `sent_by` |
| 4 | `trg_followup_agent_cannot_send` | any UPDATE to `sent` naming a service |

Layer 1 gives a readable message. Layers 3 and 4 hold when the service layer is
bypassed, and the tests prove it by driving raw SQLite with no application code
in the path:

```python
with pytest.raises(sqlite3.IntegrityError, match="The agent never sends"):
    conn.execute("UPDATE followup_drafts SET status = 'sent' WHERE id = ?", (draft.id,))
```

`trg_followup_insert_is_draft` closes the obvious way round: an INSERT arriving
with `status = 'sent'` would walk straight past every rule written on UPDATE.

The HTTP endpoint has **no default for `sent_by`**. A default would be the agent
sending under whatever name the default carried, and a test asserts a request
omitting the field fails validation.

### One test is structural rather than behavioural

```python
for path in (backend/app/scheduler).rglob("*.py"):
    assert "send_draft" not in path.read_text()
    assert "followup" not in path.read_text()
```

A scheduled job that sends a recap would satisfy every other test in the file
and break the one rule M12 states. **The capability is about what cannot
happen, so one test is about what the code does not contain.**

### The two versions of the text stay apart

`generated_body` is immutable, enforced by trigger — the same split as
`original_payload` on extractions, and for the same reason. Which half a reader
is looking at is the first question worth asking about a machine-drafted
message, and the trigger makes the answer unforgeable.

`body` returns the human's version when there is one, so **what gets sent is
what the person last saw**, and the test asserting that reads the notifier's log
rather than the draft row.

### Sent is terminal, and versions accumulate

A message a person has sent cannot be rewritten afterwards to say something they
did not send. Enforced in the service and by `trg_followup_sent_is_final`.

Each `create_draft` writes a new version. A recap drafted before three more
items were approved is a different message, and the earlier one may already have
gone out.

### There is no recap of nothing

A source with no approved items raises rather than producing an empty message.
An empty recap sent by mistake states that the meeting produced nothing, which
is a claim the system has no basis for.

---

## How it is tested

32 tests in four groups.

**What goes into a draft.** Only approved items, checked twice: once against
the structure, and once against the rendered text, asserting that no unapproved
quote appears anywhere in it. Every line's quote is present in the body.
Unowned commitments say the assignee is unspecified. Undated ones say no date
was stated. Nothing approved raises.

**The two versions.** An edit leaves `generated_body` untouched, sets
`edited_body`, flips the status, and `body` follows the human. An edit with no
editor is refused. Versions increment.

**Who may send.** A named person sends and the post reaches the notifier's log
with the edited text. A blank name is refused and **nothing is posted**. Seven
service names are refused, parametrised. Sending twice is refused. Editing after
sending is refused.

**With no Python in the path.** Five raw-SQLite tests against the triggers:
rewriting the generated text, sending with no person, sending as `scheduler`,
inserting a row already marked sent, and changing a message after it was sent.

Plus four API tests, including the one asserting the request fails validation
when `sent_by` is absent.

---

## What it does not do

- **Nothing is actually sent.** `MockNotifier` appends to
  `write_log/notifications.jsonl`. That is the point: the brief says a clean
  swappable mock earns full marks and a real integration earns none.
- **No recipient list.** The draft goes to a channel name. Who reads it is the
  notifier's problem, and per-person delivery is M13's territory.
- **No model polish pass.** It would work — render the template, ask the model
  to smooth it, then check every sentence against an approved quote before
  showing it. Not built, because the check is the hard half and the plain
  version is already correct. `L32`.
- **No thread or reply handling.** One draft, one send, no conversation.
