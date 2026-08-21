# 02 · The model layer

**Shared by:** M3, M4, M5, M8, M9
**Code:** `backend/app/extraction/llm/` and `extraction/prompts.py`
**Tests:** 29 across `test_llm_client.py` (17) and `test_llm_providers.py` (12)

---

## What it does

**Every model call in the system goes through one function**, `call_structured`.
Nothing else calls a provider, so retrying, repairing, validating, rate
limiting, caching and accounting behave identically whichever model is
configured.

```
  rate limiter — token bucket, 15/min
        │
        ▼
  response cache ──── hit ────► return (cache_hit: true)
        │ miss
        ▼
  provider.generate(prompt, json_schema)
        │
        ▼
  parse JSON ─────────── fail ──► repair prompt ──┐
        │                                          │
        ▼                                          │
  validate against Pydantic ── fail ──► repair ────┤ retry
        │                                          │
        ▼                                          │
  extra validators ────────── fail ──► repair ─────┘
        │                                    (quote verification lives here)
        ▼
  validated model                every attempt → llm_calls
```

---

## Structured output: constrained, then validated anyway

| Provider | How the schema is enforced |
|---|---|
| Gemini | `response_json_schema` + `response_mime_type: application/json` |
| Ollama | the same schema in its `format` field |
| Fake | answers from a scripted table, or the smallest document the schema allows |

**Every response is still validated against the Pydantic contract**, because a
constrained decoder makes malformed output unlikely rather than impossible, and
because the wrapper must behave identically on a provider without that feature.

The brief names schema-validated output **with** a retry loop as mandatory, not
one or the other.

`extra="forbid"` on every contract means a model inventing a field triggers a
repair rather than having its extra output silently dropped.

---

## The repair loop feeds back the actual error

Not "try again":

| Failure | What the model is told next |
|---|---|
| Not JSON | `response was not valid JSON: Expecting value at line 1` |
| Missing field | `owner: Field required` |
| Out of range | `confidence: Input should be less than or equal to 1` |
| Invented field | `ticket_id: Extra inputs are not permitted` |
| Fabricated quote | the quote that failed, plus how far into it the text stopped matching |
| Unknown message id | the ids that were not in the batch |
| Dropped message | how many came back with no label, and which |

The repair prompt truncates the offending response to 1,500 characters, because
a long malformed answer pushes the instructions out of the model's attention and
makes the repair less likely to work.

**Quote verification is a validator inside this loop**, not a filter after it.
The brief calls the substring check *"cheap and decisive"* and says to retry the
model with the failure fed back before giving up on an item.

---

## Three providers, one interface

The adapter contract asks whether a real integration could be dropped in by
writing one class and changing one line of wiring. `factory.py` is that line.

```python
if cfg.llm_provider == "gemini":  return GeminiProvider(...)
if cfg.llm_provider == "ollama":  return OllamaProvider(...)
if cfg.llm_provider == "fake":    return FakeProvider()
```

**`FakeProvider` is a real implementation, not a mock.** It goes through the
entire wrapper: schema construction, JSON parsing, Pydantic validation, quote
verification, the repair loop, the cache and the telemetry write.

It exists because **a live model cannot be made to return malformed JSON, or a
fabricated quote, or the same deferred decision twice, on demand.** Scripting
the failure is not a shortcut around testing the retry loop — it is the only way
to test it at all.

Unscripted, it answers with the smallest document its schema allows, which makes
`make llm-smoke PROVIDER=fake` a working offline dry run of the whole path.

**Honesty note:** Ollama has never been run against a live daemon on this
machine — 8 GB of RAM, and a 7–8B model alongside FAISS and Whisper is tight.
The class is written and its unreachable path is tested. `decision_log.md` L8
says so rather than implying otherwise.

---

## Prompt versioning

```
# version: 2
# capability: extract_actions
# changed: v1 scored recall 0.85 and precision 0.52 on the first measured run...
---
<body>
```

The loader returns the declared version **and** a SHA-256 of the body. The
stored tag is `2+44157f`, recorded on every extraction and every `llm_calls` row.

**Why both:** the declared version is what you talk about in a walkthrough; the
hash means an edit made without bumping the version still changes the tag, so a
measured result can never be attributed to a prompt that did not produce it.

Five prompts: `extract_actions`, `extract_decisions`, `extract_risks`,
`classify_signals`, `answer_question`.

---

## Free-tier survival

**The binding limit is per day, not per minute.** `gemini-3.6-flash` allows **20
requests a day**. One full evaluation run costs 18–24.

| Mechanism | What it handles |
|---|---|
| Token bucket, 15/min | The per-minute limit. Staying under a known limit beats discovering it |
| Exponential backoff | A 429 hit anyway, e.g. another process sharing the key |
| Response cache | The per-day cap. This is the documented workaround the ground rules ask for |

The cache key covers **provider, model, prompt text, prompt version, temperature
and the JSON schema**, so editing a prompt misses the cache automatically. A
cached result can never be attributed to a prompt that did not produce it.

**Risk acknowledged:** caching makes an eval run reproducible and could hide a
model that became unreliable. Guarded by reporting the hit rate and by
`make eval-fresh`, which bypasses it.

**A per-model quota is a legitimate second workaround.** Quotas are per model,
so switching to `gemini-3.5-flash` gives a fresh allowance. Used during
development; the committed results name the model that produced them.

---

## Accounting

One row per **attempt** in `llm_calls`, grouped by a logical `call_id`:

```
retry rate  = (attempts − distinct call_ids) / attempts
cache rate  = cache_hits / attempts
cost        = SUM(prompt_tokens), SUM(completion_tokens), SUM(latency_ms)
```

### A bug a test caught here

`test_every_attempt_is_recorded_so_the_retry_rate_is_measurable` failed with 1
row where 2 were expected.

Every attempt of one logical call wrote with the same `id`, which is
`llm_calls`' primary key. Retries collided and the second insert was rejected —
and the failure was **invisible**, because the telemetry writer swallows
exceptions by design so accounting can never break an extraction run.

Fixed by separating `id` (the attempt) from `call_id` (the request). The swallow
now logs at **warning** rather than debug, because a silent swallow hid this
once already.

---

## How it is tested

| What | Tests |
|---|---|
| Happy path, schema sent to the provider | 2 |
| Repair loop: bad JSON, missing field, out of range, invented field, custom validator, markdown fence, exhaustion | 7 |
| Accounting, including that telemetry failure never breaks a run | 2 |
| Provider failures: unavailable, rate limited | 2 |
| Caching: key sensitivity, reuse, hit flag | 4 |
| Factory selection, interface conformance, availability reporting, rate limiter, backoff | 12 |

**Backoff base is 0 in the test environment.** The rate-limit tests took the
suite from 5 seconds to 44, all of it asleep proving that the code sleeps. The
behaviour is still asserted; the waiting is not.

---

## What it does not do

- **No agent framework.** A hand-rolled dispatch loop, as the toolkit tab
  recommends: *"a hand-rolled loop you fully understand beats a framework you
  cannot explain"*.
- **No streaming, chat history, function calling or embeddings** on this
  interface. Embeddings have their own. An interface that promises more than it
  is asked for is a claim that will not survive review.
- **No token counting from a real tokeniser.** Chunk sizes estimate at
  characters ÷ 4, named as an approximation rather than importing a tokeniser
  built for a different model family.
