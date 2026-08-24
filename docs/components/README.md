# Component documentation

One document per part of the system. Each covers what the component does, which
capability it implements, the decisions that shaped it and why, how it is
tested, and what it does not do.

| # | Component | Capability | Doc |
|---|---|---|---|
| 01 | Ingestion and the consent gate | M1, M2 | [01-ingestion.md](01-ingestion.md) |
| 02 | The model layer | shared by M3–M5, M8, M9 | [02-llm-layer.md](02-llm-layer.md) |
| 03 | Chunking and grounding | shared | [03-chunking-and-grounding.md](03-chunking-and-grounding.md) |
| 04 | Extraction | M3, M4, M5 | [04-extraction.md](04-extraction.md) |
| 05 | The review queue | M6 | [05-review-gate.md](05-review-gate.md) |
| 06 | The tracker adapter | M7 | [06-tracker.md](06-tracker.md) |
| 07 | Retrieval and question answering | M8 | [07-retrieval-qa.md](07-retrieval-qa.md) |
| 08 | Chat signals | M9 | [08-chat-signals.md](08-chat-signals.md) |
| 09 | Scheduler and digests | M10 | [09-scheduler-digests.md](09-scheduler-digests.md) |
| 10 | Outcome records | M11 | [10-outcome-records.md](10-outcome-records.md) |
| 11 | The review interface | — | [11-frontend.md](11-frontend.md) |
| 12 | Evaluation | scored separately | [12-evaluation.md](12-evaluation.md) |
| 13 | Per-person digests | M13 | [13-person-digests.md](13-person-digests.md) |
| 14 | The follow-up draft | M12 | [14-followup-draft.md](14-followup-draft.md) |
| 15 | The tool-dispatch loop | multi-step tool use | [15-agent-loop.md](15-agent-loop.md) |

## Reading order

For a reviewer with twenty minutes:

1. [`../architecture.md`](../architecture.md) — the shape of the whole thing
2. [05-review-gate.md](05-review-gate.md) — the property the build is organised around
3. [12-evaluation.md](12-evaluation.md) — the numbers and how they were produced
4. [03-chunking-and-grounding.md](03-chunking-and-grounding.md) — the brief says
   chunking will be asked about

## Related documents

- [`../../README.md`](../../README.md) — capability status and measured results
- [`../../decision_log.md`](../../decision_log.md) — every decision in order, with the alternative rejected
- [`../testing.md`](../testing.md) — testing strategy and the techniques behind it
- [`../outcome_schema.json`](../outcome_schema.json) — the published M11 schema

## A note on how these were written

Every figure here was taken from the code or from a real run at the time of
writing. Where a number appears, there is a command that reproduces it. Where a
decision is recorded, the reason is the one that actually drove it, including
the cases where the first attempt was wrong and a test caught it — those are
kept rather than tidied away, because they are the useful part.
