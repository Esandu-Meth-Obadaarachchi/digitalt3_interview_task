# 15 · The tool-dispatch loop

**Capability:** M14 (the catalogue's *"multi-step tool use, if you need it"*)
**Code:** `backend/app/agent/` — 3 modules
**Tests:** 18 — `test_agent.py`

```
instruction
    │
    ▼
  ┌──────┐   tool calls    ┌──────┐
  │ plan │ ───────────────► │ act  │
  │      │ ◄─────────────── │      │
  └──┬───┘   observations   └──────┘
     │
     │ no tool call, or budget spent
     ▼
   answer
```

Two nodes, one conditional edge, one loop. LangGraph `StateGraph`.

---

## Why a framework at all

The brief recommends plain functions plus a tool-dispatch loop and allows a
framework *"if you already know one"*. The loop itself is four lines either way,
so the framework has to earn its place on something else. It earns it on three:

**The state is a value.** Every message, every observation and the step count
come back in one object, so the trace shown to a reviewer is what the loop
executed rather than a log written alongside it.

**The budget sits on an edge.** It is enforced where it is visible, in the graph,
rather than inside a `while` condition somebody can forget.

**Tool schemas come from the docstrings.** `@tool` turns a typed Python function
into a JSON schema the model can call, so the description a model reads and the
function a developer reads are the same text.

The cost is three dependencies and a second path to the model, which is a real
cost and is recorded as `L39`.

---

## What the loop cannot do

This is the design decision, and it is about absence.

| | |
|---|---|
| Tools that read | 9 |
| Tools that write | **1**, and it writes `pending` |
| Tools that approve, write outward or send | **0** |

There is no tool for approving an extraction, writing to the tracker or sending
a follow-up. **The loop reads freely and may propose. A person still holds all
three gates.**

An agent able to approve its own proposals would make the approval gate
reachable by a model deciding it was confident. The catalogue asks for
multi-step tool use, not for autonomy over the gate.

Two tests hold the line, and the second matters more:

```python
# by name
for name in TOOLS_BY_NAME:
    assert not any(w in name for w in {"approve", "write", "send", "sync", ...})

# structurally, because a tool could be added tomorrow
source = pathlib.Path(agent_tools.__file__).read_text()
assert "tracker.service" not in source
assert "followup" not in source
assert "queue.approve" not in source
```

---

## The toolbelt

| Tool | Reaches |
|---|---|
| `list_sources` | every ingested source and its status |
| `search_transcripts` | segments and approved extractions, by BM25 and dense |
| `search_chat_messages` | `chat_messages_fts`, which the transcript index does not cover |
| `read_transcript` | consecutive turns, for context around a hit |
| `list_extractions` | filtered by source, type and status |
| `review_queue_summary` | what is waiting for a human |
| `read_chat_messages` | classified messages, filtered by channel or class |
| `answer_with_citations` | the M8 pipeline, already quote-verified |
| `list_tracker_items` | what the tracker holds |
| **`propose_action_item`** | **writes one pending row, and only with a verified quote** |

`propose_action_item` is held to the same evidence standard as the model reading
a chunk. The quote must be a literal substring of the source or the proposal is
refused and nothing is stored:

```
REFUSED: that quote does not appear in the source. Copy it character for
character from the transcript, or propose nothing.
```

---

## What the first real run exposed

The tests all passed against the scripted planner. Then the loop met Gemini.

**The answer came back as a JSON blob.** Gemini returns content as a list of
parts rather than a string, so the answer reached the interface beginning
`[{"type": "text", …}]`. The stub returns plain strings, so nothing caught it.
Fixed with `_text_of`.

**The loop spent its whole budget browsing.** Five steps, four of them single-word
searches, and no answer. The planner had no way to see its own budget: nothing in
the conversation said how many calls it had. Every observation now carries
`[step 2 of 4, 2 left]`, and the system prompt says a run ending on the budget is
a run that browsed instead of deciding.

**And the answer was wrong.** Asked which meetings mention the staging
environment being down, it said none did. A stored chat message says exactly
that:

> msg_003 · Marcus Webb · *"Heads up: the staging environment is down. The SSL
> certificate expired overnight."*

The cause was a gap between two things that both look like search.
`search_transcripts` covers segments and approved extractions, so it cannot see
chat. `read_chat_messages` takes filters rather than a query, and ordered by
channel and time its first twenty rows were all from the other channel. The
agent looked in both places it had and neither could reach the message.
`chat_messages_fts` had existed since Phase 7 and nothing searched it.

**Worth keeping as a story.** The wrong answer was confident, quoted real text
and was produced by a system whose whole point is verifiable output. What caught
it was reading the answer against data whose contents were already known.

After the fix, the same question:

```
[1] search_transcripts(query="staging")
[2] search_chat_messages(query="staging")
[3] search_transcripts(query="expired")
[4] search_transcripts(query="down")
→ stopped: answered, 4 of 5 steps

"…mentioned in #proj-meridian-dev, raised by Marcus Webb:
 'Heads up: the staging environment is down…'"
```

---

## How it is tested

18 tests, and the planner is scripted in every one. A test asserting the model
chose the right tool would be measuring the model, and that belongs in the eval
harness.

**What it can reach**, three tests: no tool name sounds like a gate, the module
imports nothing that writes outward, every tool has a real description.

**The mechanics**, five: steps recorded in order with their arguments and
observations, the budget stops the loop and still answers, an unknown tool is
reported back rather than crashing, a failing tool does not end the run.

**Proposing**, three: a proposal lands as `pending` with `quote_verified`, a
quote the source does not contain is refused and nothing is stored, an unknown
source is refused.

**The HTTP surface**, three, including that a request asking for 5,000 steps
gets 20.

**The two real-run defects**, two, so neither returns quietly.

---

## What it does not do

- **No memory between runs.** Each instruction starts clean. LangGraph
  checkpointing would give it a thread, and nothing here needs one yet.
- **No parallel tool calls.** The graph executes whatever the model asked for in
  one turn, in order. Fine at this scale.
- **No verification of the final prose.** Individual tool observations are
  evidence, and `answer_with_citations` verifies its own quotes, but the closing
  paragraph is checked by nobody. `L40`.
- **It is not in the eval harness.** Agent behaviour is demonstrated, not
  measured. Scoring a loop needs a golden set of instructions and expected tool
  sequences, which does not exist. `L41`.
