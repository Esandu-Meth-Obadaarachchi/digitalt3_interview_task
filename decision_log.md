# Decision and assumption log

Scope decisions, assumptions about ambiguous requirements, and known
limitations. Appended to as the build proceeds. Newest phase last.

---

## Phase 0 — Foundation

### Decisions

**D1. Python 3.11, not 3.13 or 3.14.**
Both newer versions are installed on the build machine. 3.11 is the newest
release with settled wheels for `faiss-cpu`, `sentence-transformers` and
`ctranslate2` on Apple Silicon. Verified by resolving the full dependency tree
before committing to it, rather than discovering a missing wheel in Phase 6.

**D2. Raw `sqlite3` over SQLAlchemy.**
The brief requires the schema be visible in the repo rather than created ad hoc
at runtime. With raw SQL, `schema.sql` *is* that artefact. The FTS5 virtual
tables, their sync triggers and the consent/approval triggers are native SQL
and would need a hand-written migration under an ORM regardless, so the ORM
would add an abstraction without removing any work. Pydantic already provides
the typed contracts an ORM would otherwise justify.
*Cost:* a future Postgres move would mean rewriting the queries. Acceptable,
since the brief names SQLite as the default choice and multi-tenancy is
explicitly out of scope.

**D3. Gating enforced in the database, not only in the service layer.**
The rubric's red flag for human-in-the-loop gating is "approval exists in the
UI but is bypassable via the API". Enforcing in Python alone leaves the same
weakness one layer down. Three triggers and one unique constraint mean the
consent gate, the approval gate and write idempotency hold even against direct
`sqlite3` CLI access. The service layer repeats the checks so the API returns a
readable error rather than an `IntegrityError`.
*Cost:* the rules exist in two places and could drift. Mitigated by
`backend/tests/test_schema_guarantees.py`, which asserts the database refuses
each forbidden operation directly.

**D4. Direct messages and noise are unstorable, not merely unstored.**
`CHECK (is_direct_message = 0)` on `chat_messages`, and `noise` omitted from
the classification constraint. Golden case 7 requires zero DM records in the
store. A constraint makes that a property of the schema rather than a claim
about a code path.
*Consequence:* the count of excluded direct messages is recorded in
`ingestion_reports`, since the messages themselves leave no trace by design.

**D5. `expired` as a fourth review state.**
The rubric asks for "a safe default on timeout or no response" under
human-in-the-loop gating. A pending extraction older than `PENDING_EXPIRY_HOURS`
is swept to `expired`, which the approval-gate trigger treats exactly like
`pending`: not writable. The safe default is refusal, never an implicit
approval.

**D6. `original_payload` immutable, `payload` editable, status terminal.**
The review surface has to show what the model said beside what the human
changed, so the model's first output is protected by trigger. Approved,
rejected and expired are terminal, which closes the route where a rejected
payload is reopened and pushed through.

**D7. The database is a build artefact, not source.**
`make seed` drops and rebuilds it from `schema.sql`. Seed data is disposable,
so migration tooling would never be exercised and would only add a second place
where the schema is defined.

**D8. `SourceMetadata` accepts field aliases.**
Supplied metadata is an external shape, so the contract accepts `date` or
`meeting_date` and `consent` or `consent_flag`, normalising to one internal
name. The alias list is the only place that mapping lives, which keeps the
external shape out of the rest of the system.

### Assumptions

**A1.** Reviewer identity is a supplied name, not an authenticated user.
Authentication and multi-tenancy are explicitly out of scope in the brief. The
approval audit records whatever name the reviewer supplies. A real deployment
would take this from the session.

**A2.** Consent is a property of a source, not of an individual participant.
The brief supplies one consent flag per source and says nothing about
per-speaker consent, so the system does not model it.

### Known limitations

**L1.** No database migrations. Changing `schema.sql` requires `make seed`,
which discards existing data.

**L2.** Direct dependencies are pinned exactly, transitive ones are not. A full
lock file was judged not worth the tooling for a build of this size.

### Deviations from the supplied build spec

The planning document at `docs/planning/CLAUDE_CODE_BUILD_SPEC.md` proposed
Streamlit and SQLite FTS5 alone. This build uses React for the review surface
and hybrid retrieval (FTS5 plus FAISS over `all-MiniLM-L6-v2`, fused with
Reciprocal Rank Fusion) with all three modes measured side by side by the eval
harness. Rationale for the retrieval change is recorded in Phase 6. The brief
warns that "keyword search that works and is measured beats a vector store that
is never evaluated", which is met by measuring all three rather than by
avoiding the vector store.

---

## Phase 1 — Ingestion and the consent gate

### Decisions

**D9. The consent gate fires on metadata, before the file is opened.**
The capability test requires the non-consented meeting is "never transcribed,
never sent to a model, and produces zero extracted items". Checking after
parsing would satisfy the wording while the content sat in memory and in the
store. The gate therefore runs first, on the metadata alone, and a refused
source never reaches the read step.
*Evidence, not assertion:* a refused source's ingestion report carries
`bytes_read: 0` and `content_hash: null`. That number is the demo's proof.
*Layers:* metadata gate, then a second check in the extraction service before
any model call, then `trg_consent_gate_insert` in the database. Any one
satisfies the requirement. Together, no code path reaches an extraction for a
non-consented source.

**D10. Defects are severity-graded, not binary.**
ERROR-level defects (truncation, undecodable bytes, no parseable segments,
unknown format, malformed structure) reject the file whole and store nothing.
WARNING-level defects (missing speaker label, missing timestamp, non-monotonic
timestamps, empty segment) travel with the source and surface in review.
The brief requires the deliberately malformed sample be rejected with a clear
reason and not corrupt the store. It does not require rejecting every imperfect
file, and a transcript with one unlabelled line is realistic input.

**D11. Truncation detection is a heuristic, and is documented as one.**
The rule: the final segment does not end in terminal punctuation. On the
committed samples this cleanly separates `malformed_meeting.txt` (ends on
"upd") from the three valid transcripts (all end on a full stop).
*False-positive mode:* a recording that genuinely ends mid-thought would be
rejected. Mitigated by making the check switchable per source
(`check_truncation=False`), which is tested.
*Alternative considered:* checking whether the final token is a dictionary
word. Rejected as more machinery for no more certainty.

**D12. An unlabelled line keeps `speaker: null`.**
In `malformed_meeting.txt` three lines carry a timestamp but no speaker, and
each directly follows a line by the same person, so inheriting the speaker
above would look correct almost every time. It would still be a guess, and
rule 1 of the brief forbids inventing a speaker. Each such line records a
`missing_speaker_label` warning stating exactly that.

**D13. A single-token speaker candidate is accepted only against metadata.**
Splitting on the first colon turns "Note: the deadline moved" into a speaker
called Note, which a test caught. Two signals are used instead: the candidate
matches a participant named in the source metadata, or the candidate is
name-shaped (two to four capitalised tokens, no sentence punctuation, first
word not a document-structure word such as note, action, agenda, summary).
A lone capitalised word is too weak a signal on its own.
*Consequence:* a transcript with no participant metadata that writes
"Sarah: ..." loses the speaker label. That is the safe failure. The unsafe
failure is inventing one.

**D14. A first name shared by two participants is left unresolved.**
The sample data plants Priya Sharma and Priya Menon in the same meeting, so a
bare "Priya" is ambiguous. The speaker lookup registers a first name only when
exactly one participant owns it.
*Consequence:* cross-source identity resolution is a separate, evaluated
problem, deferred to the Phase 11 stretch work where the merge rule can be
measured rather than assumed.

**D15. One definition of "the text of a source".**
`source_text = " ".join(whitespace-normalised segment texts, in order)`.
Quote verification checks against that string, and `char_start` / `char_end`
index into it. Because both use one definition, an offset is checkable by hand
and a quote spanning two segments still verifies. Speaker labels and timestamps
are excluded deliberately: a verbatim quote must be words somebody said, not
"[00:02:17] Priya Sharma:".
*Guarded by test:* every hand-labelled golden quote is asserted to be a literal
substring of this exact string, so ground truth cannot drift from the system
measured against it.

**D16. The rejection message names the most diagnostic defect, not the first.**
A file often trips several blocking checks at once. Reporting "only 1 segment
parsed" when the real problem is a truncated recording sends a reader looking
in the wrong place, so blocking defects are ranked and the summary leads with
the most specific.

**D17. Format fixtures are committed but not registered as sources.**
The brief names txt, vtt and json but supplies only .txt, so two parsers would
have been asserted rather than demonstrated. `sample_data/format_fixtures/`
holds the same client status call as WebVTT and as JSON, generated by a
committed script, and the suite proves all three formats normalise to identical
segments. They are absent from `sources.json` on purpose: ingesting the same
conversation three times would double-count it in the extraction corpus and
skew every golden metric.

**D18. Warnings are errors in the test suite.**
One named third-party exception (Starlette's nudge towards httpx2 on importing
its test client). `pytest-asyncio` was removed rather than configured, because
nothing in the suite is asynchronous and an unused dependency is a claim about
the build that is not true.

### Assumptions

**A3.** Transcript timestamps are wall-clock offsets from the start of the
recording, not absolute times. The samples use `[HH:MM:SS]` with no date, and
nothing in the brief suggests otherwise.

**A4.** Re-ingesting a source replaces it. Sources are keyed by a stable id
supplied in metadata, so a second ingest of the same id is a correction rather
than a new meeting.

### Known limitations

**L3.** Speaker diarisation is out of scope per the brief. Speaker labels come
from the source or are absent. Nothing infers who spoke.

**L4.** The truncation heuristic is a heuristic. See D11.

**L5.** Audio ingestion is not built yet. M1 is marked Partial in the README
for that reason, and audio arrives in Phase 9.

---

## Phase 2 — The model layer

### Decisions

**D19. Structured output is constrained at the decoder, then validated anyway.**
Gemini receives the JSON schema through `response_json_schema` with
`response_mime_type="application/json"`. Ollama receives the same schema in its
`format` field. Every response is still parsed and validated against the
Pydantic contract.
*Why both:* a constrained decoder makes malformed output unlikely rather than
impossible, and the wrapper has to behave identically on a provider without
that feature. The brief names schema-validated output with a retry loop as
mandatory, not as one or the other.

**D20. Failures feed the actual error back, not a generic retry.**
A missing field produces `owner: Field required`. An out-of-range confidence
names the field and the bound. A rejected quote produces the quote itself and
an instruction to copy exact text. The repair prompt truncates the offending
response to 1500 characters, because a long malformed answer pushes the
instructions out of the model's attention and makes the repair less likely.

**D21. Quote verification is a validator inside the retry loop, not a filter
after it.**
The brief calls the substring check "cheap and decisive" and says to retry the
model with the failure fed back before giving up on an item. Wiring it as one
of the wrapper's `validators` means a fabricated quote is a validation failure
like any other, so it is repaired by the same mechanism that repairs a missing
field. Phase 3 supplies the validator; Phase 2 built the slot.

**D22. `extra="forbid"` on every contract.**
A model that invents a field is a model drifting from the schema. Forbidding
extras turns that into a validation failure that triggers a repair, rather than
silently discarding output that might have mattered.

**D23. One row per attempt in `llm_calls`, grouped by a logical call id.**
Retry rate, cache hit rate and per-source token cost then come from the store
rather than from an estimate. A test caught the first version of this: every
attempt wrote with the same primary key, so retries collided and the retry rate
silently read as zero. The telemetry writer swallows exceptions by design, so
the failure was invisible until asserted on.
*Consequence:* the swallow now logs at warning rather than debug.

**D24. Prompts are versioned files carrying a declared version and a body hash.**
The anti-pattern the brief names is prompts scattered inline, already drifted,
impossible to version or evaluate. One file per capability, loaded at runtime,
with a header declaring version, capability and what changed. The loader also
hashes the body, and the stored tag is `version+hash6`.
*Why both:* the declared version is what a human talks about in a walkthrough;
the hash means an edit made without bumping the version still changes the tag,
so a measured result cannot be attributed to a prompt that did not produce it.

**D25. Chunking: whole segments, whole-segment overlap, context header.**
1. Chunks are built from whole segments, never split mid-segment. A segment is
   one person's turn, and splitting it separates a commitment from the words
   that make it one and produces a quote that is not a substring of any line.
2. Overlap is whole segments, not a character count. A commitment is usually
   made across two turns, one person asking and another agreeing. A boundary
   between them leaves both chunks wrong in the same way. Repeating the last
   few whole turns means the pair appears complete in at least one chunk.
3. Every chunk carries a non-quotable context header naming the meeting, date,
   full participant list and time range. Owner attribution is golden case 3,
   and without the participant list the model cannot resolve "James" to James
   Liu or know that two people called Priya are present.
4. Timestamps and speaker labels are rendered into the chunk text, since the
   model must return them. An unlabelled speaker renders as `UNSPECIFIED`, the
   same token the model must output for an unknown owner, so the transcript and
   the prompt agree on what "not stated" looks like.
*Cost:* overlap causes the same commitment to be extracted more than once.
Deduplication in Phase 3 is the accepted price.

**D26. Token counts are estimated as characters / 4.**
Naming the approximation is more honest than importing a tokeniser built for a
different model family and implying a precision that is not there. Chunks are
sized well below any provider's context window, so the estimate has room to be
wrong.

**D27. Response caching on by default, keyed by everything that could change
the answer.**
Provider, model, prompt text, prompt version, temperature and the JSON schema.
Editing a prompt therefore misses the cache automatically.
*Risk acknowledged:* caching makes an eval run reproducible and could also hide
a model that has become unreliable. Guarded by reporting the hit rate in the
eval output and by a cache bypass that proves the committed numbers reproduce
against live calls.

**D28. A token bucket in front of the provider, not just backoff.**
The Gemini free tier allows 15 requests per minute. Staying under a known limit
is better than discovering it. Backoff remains for the case where the limit is
hit anyway, for instance because another process shares the key.

**D29. `FakeProvider` is a real implementation, not a mock.**
Tests exercise the entire wrapper, including retry and repair, with no network
and no key. Scripting a malformed response is the only way to test the repair
loop at all, since a live model cannot be made to return broken JSON on demand.
An unscripted call answers with the smallest document its schema allows, which
lets the whole pipeline be dry-run offline with `make llm-smoke PROVIDER=fake`.

**D30. Two providers were written, not one plus a claim.**
The adapter contract asks whether a real integration could be dropped in by
writing one class and changing one line of wiring. Gemini and Ollama both
implement `LLMProvider`, and the factory is the only place that knows the
difference.

### Assumptions

**A5.** Temperature 0 for extraction. Repeatable output matters more than
variety, and every golden metric assumes the same input gives the same answer.

**A6.** Gemini's free tier limit is 15 requests per minute. Configurable via
`GEMINI_REQUESTS_PER_MINUTE` if that changes.

### Known limitations

**L6.** Token counts are estimates. See D26.

**L7.** The response cache is not bounded. A long build could accumulate a
large `data/llm_cache/`. `make cache-clear` empties it.

**L8.** Ollama has not been exercised against a live daemon on this machine
(8 GB of RAM, and a 7-8B model alongside FAISS and Whisper is tight). The
provider is written, its unreachable path is tested, and the README says so
rather than implying it has been run.

---

## Phase 3 — Action extraction, the review queue, and the first measurement

### Decisions

**D31. Quote verification runs inside the retry loop, not after it.**
The validator is handed to the model wrapper, so a quote that is not a literal
substring of the transcript is a validation failure like a missing field, and
the model is told which quote failed and how far into it the text stopped
matching. The brief calls the substring check "cheap and decisive" and asks
that the model be retried with the failure fed back before an item is given up
on.

**D32. A quote that cannot be verified is flagged, not discarded.**
The validator gets the full retry budget. If the model still cannot produce a
literal quote, the last schema-valid response is taken and the offending items
are stored with `quote_verified = 0`, sorted to the top of the queue, and
blocked from approval without an explicit override and a written reason.
*Why not discard:* it would make the fabricated-quote metric zero by
construction. The brief warns that a harness reporting everything passing
usually means the cases were too easy. The number has to be able to be non-zero
or it is not a measurement.
*Cost:* the extra call to retrieve the flagged items is free, because the same
prompt is served from the response cache.

**D33. Deduplication needs two different rules, because duplication has two
different causes.**
*Within a region:* chunk overlap causes the same words to be read twice. Caught
by quote-span overlap of at least half the shorter span AND task-description
containment of at least 0.4. Both are required. Span alone would merge two
commitments made in one sentence ("I'll write the tests and Priya will review
the schema"); task alone would merge two people committing to similar work in
different parts of a meeting.
*Across the meeting:* a closing recap restates every commitment thousands of
characters from where it was made. Span comparison cannot see that far. Caught
instead by the same NAMED owner and task containment of at least 0.7, with no
span requirement. The threshold is higher because there is no span evidence
supporting it, only the wording.
*Measured, not guessed:* on the sample data 0.7 merges every recap restatement
and merges nothing it should not. The nearest non-duplicate pair by the same
owner scores 0.0. UNSPECIFIED owners are excluded, because "the same owner" is
meaningless when nobody was named.
*Containment rather than Jaccard,* because the model describes the same
commitment at different lengths in different chunks and Jaccard penalises that.
*When the rules disagree, both candidates are kept.* A duplicate in the review
queue is visible and dismissed in one click. A wrong merge is invisible and has
destroyed a real commitment.
*The survivor is kept whole.* Fields are never mixed between two candidates,
because a record assembled from two model outputs is one that no model produced,
and neither its quote nor its owner could then be traced to a single place.
Confidence ties break toward the earlier quote, so a recap never wins over the
moment the commitment was made.

**D34. Dates resolve in place to ISO, with provenance kept alongside.**
`due_date` holds an ISO date or UNSPECIFIED, which is the clean contract for
anything downstream. `due_date_type`, `due_date_stated` and `due_date_rule` sit
beside it.
*Why the provenance is not optional:* golden case 4 measures invented dates, and
a resolved date is otherwise indistinguishable from one the transcript stated.
Without provenance the metric cannot be computed at all.
*Anchored to the meeting date, never to today,* so re-running the harness next
month produces the same answer.
*What no rule covers is not resolved.* "Early October", "soon", "next sprint"
and "after the audit" stay UNSPECIFIED. An approximate date presented as a real
one is exactly the failure golden case 4 probes for.

**D35. `expired` is the safe default, and the expiry sweep takes an injectable
clock.** The rubric asks for "a safe default on timeout or no response". An
unreviewed item ages out to `expired`, which the approval-gate trigger treats
exactly like `pending`: not writable. The clock is injectable so the behaviour
is demonstrable without waiting three days.

**D36. An unverified quote needs an override and a written reason.**
The database would accept the row. A distracted click should not. The reason is
recorded in the audit trail prefixed as an override, so a later reader can tell
a considered acceptance from an ordinary approval.

**D37. The fabricated-quote count is recomputed, not read.**
The harness re-checks each stored quote against the stored transcript rather
than trusting `quote_verified`, so the most important number in the submission
does not depend on the code path that set the flag.

**D38. A stub run refuses to be mistaken for a measurement.**
Running the harness against the deterministic provider prints a NOT A
MEASUREMENT banner and writes no results file, because that provider answers
from the golden file itself. Its perfect scores prove the pipeline and the
scoring code and say nothing about model quality.

**D39. The model is pinned to an exact version.**
`gemini-2.0-flash` returned 404 mid-build: Google had retired it. Replaced with
`gemini-3.6-flash`, pinned rather than using the `gemini-flash-latest` alias, so
a committed evaluation result names the model that produced it.

**D40. Golden labels were not added after seeing model output.**
Six of the seven remaining false positives are genuine commitments the
hand-labelled set does not contain. Adding them now would be fitting ground
truth to output, so precision is reported as a lower bound instead, with the
reason stated in the README.

### Assumptions

**A7.** A commitment restated by the same named person later in the same
meeting is the same commitment, not a second one. This is what makes the
cross-region deduplication rule safe.

**A8.** Temperature 0 is requested but not relied upon. Gemini returned
different action sets for the same chunk minutes apart, so the response cache
is what makes an evaluation run reproducible, not the temperature setting.

### Known limitations

**L9.** Precision measures 0.63 and is a lower bound. The golden set is
incomplete. With more time, a second person would label both transcripts blind.

**L10.** Owner accuracy is 0.90, not 1.00. One commitment whose owner the
golden set names comes back UNSPECIFIED. Abstaining where attribution was
possible is the safe direction to be wrong in, and it is still wrong.

**L11.** The extraction pipeline is not deterministic, because the model is not.
See A8.

**L12.** Extraction takes roughly 140 seconds for two transcripts across six
chunks against the hosted free tier. Acceptable for a batch tool, and the
per-source cost and latency are recorded in `llm_calls` for anyone who wants to
optimise it.

---

## Phase 4 — The approval gate and the tracker adapter

### Decisions

**D41. The interface names four operations, and each has a caller.**
`create_item`, `get_item`, `list_items`, `transition`. The brief's illustrative
example also lists `add_comment`; it is absent because no capability in this
build comments on a ticket, and the same contract warns that a capability
implied but not exercised reads as padding. A test asserts the abstract method
set, so widening it silently is not possible.

**D42. The approval gate is not in the adapter.**
It sits in the tracker service above the interface and in a database trigger
below it. Putting it in the adapter would mean every future implementation had
to reimplement it, and the one that forgot would be the one that mattered.

**D43. `tracker_items` is separate from `tracker_writes`.**
`tracker_items` is what the tracker holds, including a seeded backlog the agent
never created. `tracker_writes` is our audit of what we put there. Conflating
them would mean the agent could not distinguish its own writes from somebody
else's tickets, which is exactly the position a real integration is in.

**D44. The write log belongs to the agent, not to the tracker.**
Recording what the agent attempted is an audit of the agent, so it works
identically whichever adapter is configured and a real integration inherits it
for free. JSONL rather than a table, because during a walkthrough a log can be
read aloud and a table has to be queried.
*Found by a test:* an earlier version put `log_attempt` on `MockTracker`, which
made the tracker service import a concrete implementation. That is the mock's
shape leaking into agent logic, the specific thing the adapter contract
penalises. `test_nothing_above_the_interface_imports_the_mock` now asserts that
only `factory.py` names a concrete adapter.

**D45. Foreign data is not normalised.**
`TrackerItem` sets `str_strip_whitespace=False`, unlike every other contract
here. A test caught the base model silently turning `"In Progress "` into
`"In Progress"`, which hid exactly the mess the adapter contract requires the
agent to cope with. Our own contracts strip; somebody else's data is preserved
as found. Status filtering compares the trimmed, lowered form instead, because
that is where the tolerance belongs.

**D46. Every attempt is recorded, including the refused ones.**
`tracker_write_attempts` and the JSONL log both carry created, deduplicated and
blocked. A log that recorded only successes could not prove a gate ever fired,
and proving it is the point.

**D47. Approval writes through, and a failed write does not undo the approval.**
The task catalogue gives M7 the trigger "on approval". The write happens after
the approval transaction commits. If it fails, the human decision stands and
`sync_approved` retries on its next run. An approval that silently reverted
because a downstream system was unreachable would be worse than one that is
merely not yet written.

**D48. A compensating delete, not a distributed transaction.**
The adapter and our audit sit either side of a system boundary and cannot share
a transaction. If the audit insert fails after the item was created, the item is
deleted and the error re-raised. A real integration has the same problem and the
same answer. Reached only if a database-level gate refuses a write the service
layer already allowed.

**D49. An UNSPECIFIED owner becomes an unassigned ticket.**
The abstention is carried forward rather than resolved. A ticket assigned to
nobody is a correct record of a commitment nobody claimed, and inventing an
assignee at the write stage would undo the discipline the extraction stage
maintained. The ticket is labelled `needs-owner` so it is findable.

**D50. There is no endpoint that writes an arbitrary payload to the tracker.**
The only route in is an approved extraction. An endpoint accepting a free-form
item would be the exact hole the rubric describes, however convenient it would
be for testing.

### Assumptions

**A9.** A tracker reference is opaque to us. `MOCK-n` is the mock's format;
nothing outside the mock parses it, and the next reference is derived from the
highest existing number rather than a row count so it stays correct when the
seeded items are not contiguous.

### Known limitations

**L13.** Calling the adapter directly, bypassing the tracker service, creates
an item with no audit row. It is recorded as a boundary rather than claimed to
be impossible: the item exists but has no `tracker_writes` row, so the
`written_by_agent` accounting and the write log both show it for what it is.
A test documents this.

**L14.** Only the tracker adapter exists. The document store (M11) and the
notifier (M10) are built in Phase 8 where they have callers. The anti-patterns
tab is explicit that an empty file named after an integration implies work that
does not exist, so they are absent from the tree rather than stubbed.

---

## Phase 4 addendum — what the free tier taught

**D51. The harness refuses to write results from an incomplete run.**
Discovered by doing it. A three-run evaluation exhausted the Gemini free tier's
daily cap, every chunk failed with a 429, and the harness scored the empty
result at recall 0.00 and overwrote a good `eval/results.txt` with it.
Committing that file would have been fabricated evaluation results, which the
rubric lists as an automatic failure, and nothing in the process would have
caught it.
Any failed chunk now sets `incomplete_reason`, the run prints an INCOMPLETE
banner, no results file is written, and the exit code is 2. A single transient
429 absorbed by the retry loop does not count as a failure, and a test pins that
distinction so the guard cannot become over-eager.

**D52. The binding free-tier limit is per day, not per minute.**
`gemini-3.6-flash` allows 20 requests a day. One evaluation run costs six. The
token bucket handles the per-minute limit and does nothing for this one.
*The workaround, which the ground rules ask to be documented:* the response
cache. `make eval` reuses cached responses so a re-run is free, and the cache
key covers the prompt version so a prompt edit always misses it.
`make eval-fresh` is for once per prompt revision, not for casual use.

**D53. Repeated-run reporting exists, and reports the worst run.**
`make eval-repeat` runs the whole pipeline N times with the cache bypassed and
reports each metric as a range. A target counts as met only when every run met
it: a system that sometimes clears the bar is not a system that clears the bar.
*Not used for the committed numbers,* because the daily quota cannot pay for
three runs. The README says so rather than presenting a single run as more than
it is.

**D54. Backoff is configurable and set to zero in tests.**
The rate-limit tests took the suite from 5 seconds to 44, all of it spent
asleep proving that the code sleeps. `LLM_BACKOFF_BASE_SECONDS=0` in the test
environment. The behaviour is still asserted; the waiting is not.

### Known limitations

**L15.** The committed evaluation is one run of a non-deterministic system.
Between two runs, owner accuracy moved 0.90 to 1.00 and invented dates moved 0
to 1. Recall, precision and the fabricated-quote count were stable. With quota
for three runs the committed figure would be the worst of them.

**L16.** 20 model requests a day is a hard ceiling on how often the numbers can
be refreshed against live calls.

---

## Phase 5 — Decisions and risks

### Decisions

**D55. The extraction pipeline was lifted, not predicted.**
M3, M4, M5 and M9 differ in three things: the prompt, the contract the model
must satisfy, and how a validated item becomes a stored payload. Those are an
`ExtractionSpec`; everything else lives in `pipeline.py`.
Extracted after M3 was working rather than designed up front. The deduplication
thresholds, the quote-repair fallback and the accounting all changed twice
during Phases 3 and 4, and copying them into three more modules would have
meant four places to fix whatever the harness found next.

**D56. The decisions prompt is organised around the negative case.**
M4's capability test is not "find the decisions", it is that the
proposed-then-deferred item is NOT recorded. The list of what is not a decision
therefore comes before the rules, with deferral first on it. The trap is real:
in the client status call a strong preference for one analytics provider is
voiced immediately before "I think we should defer this decision".
One positive rule that is easy to get backwards: a deferral is sometimes itself
a decision, and a different one. The hotel kickoff postpones the permanent
product name and settles on a working name in the same breath. The working name
IS a decision.

**D57. Severity must be defensible from the quote alone, and that is asserted.**
M5's test asks whether severity is defensible, not whether it is correct. A
reviewer checking it holds only the quote. So the prompt states that test back
to the model, and case M5b enforces it: every stored high-severity risk must
quote a stated consequence. Every risk in this corpus concerns healthcare
compliance or payments and would sound serious whatever was said, so a model
reaching for high on subject matter alone would otherwise score correctly for
the wrong reason.

**D58. Cross-region deduplication is switched off for risks.**
The rule merges the same named person committing to the same thing in two
places, which is right for actions and decisions where a closing recap restates
them. It is wrong for risks: the person named on two risks is usually the
person who noticed both, not the person recommitting to one. Merging would
silently discard a real concern, and a lost risk is worse than a duplicate one
a reviewer dismisses in a click.

**D59. Golden case 5 is asserted in both directions.**
A negative test that cannot be made to fail proves nothing. The scripted
provider takes `include_deferred=True` and the suite asserts that the deferred
item does then reach the store. Without it the passing test could be passing
because the extraction never ran.

**D60. A wrong golden label is corrected; a label a model disagrees with is not.**
The harness reported an invented date on `action_sp_08` in three runs out of
four. The label was wrong: Marcus says "I'll run the baseline tests this week",
so UNSPECIFIED denied words the transcript contains.
The test applied is whether the correction can be justified from the transcript
alone, with no reference to model output. D40 still stands and this does not
weaken it.
Having found one, every remaining action labelled UNSPECIFIED was audited by
scanning the transcript around its quote for stated timings. Two more were
wrong (`action_sp_03`, `action_sp_06`) and two were checked and left alone
(`action_cs_05`, where "by end of day" belongs to the poll rather than the
coordination, and `action_hk_06`, where "talk tomorrow" is a farewell).
Only `action_sp_08` changes a measured outcome. The other two state timings no
rule can resolve, so UNSPECIFIED after resolution is still correct for them.

**D61. `--capabilities` scopes an evaluation run.**
Not a convenience. One full run over three capabilities and two transcripts
costs eighteen model requests against a free-tier allowance of twenty a day.

### Assumptions

**A10.** A commitment and its acceptance are one action, and the timing stated
in either belongs to it. This is what makes the `action_sp_08` correction
right: Sarah raises the load testing, Marcus accepts it and says "this week",
and that is one action with a stated due date.

### Known limitations

**L17.** The golden set has now been wrong twice, once about a meeting date and
once about three due dates. It was derived from LLM-generated transcripts,
which the README states. Both errors were found by the harness rather than by
review, which is an argument for the harness and not for the labels.

**L18.** M4 and M5 have been measured on one run each. Recall of 1.00 on three
decisions and two risks is a small sample and should be read as "nothing was
missed in this run", not as an accuracy claim.

---

## Phase 6 — Hybrid retrieval and question answering

### Decisions

**D62. All three retrieval modes are measured, on every run, at no model cost.**
The brief warns that keyword search that is measured beats a vector store that
is never evaluated. This build answers that by measuring rather than by
avoiding the vector store. Case 6c needs no model calls, so it runs regardless
of quota.

**D63. Reciprocal Rank Fusion, by rank rather than by score.**
BM25 scores and cosine similarities are not comparable quantities. Any weighted
sum of them needs a normalisation constant invented out of nothing, and that
constant would then be tuned on the golden set and quietly reported as a
result. RRF has no such parameter. k is left at the conventional 60 for the
same reason.

**D64. Hybrid stays the default even though dense currently measures better.**
Measured: dense reaches the cited segment in the top three for 5 of 5 questions
against hybrid's 4 of 5, level on mean rank at 2.0. The honest reading is that
five questions cannot separate them.
Hybrid is kept on an argument that is NOT measured: the two fail in different
directions, and keyword catches exact tokens, names, dates and identifiers,
where dense is weakest. None of the five golden questions probes that.
*What was deliberately not done:* adding questions designed to favour hybrid
after seeing it lose. That is fishing. With more time the fix is more golden
questions, written before running anything, deliberately including exact-token
lookups.
*If forced to choose on the evidence available today, dense would win.* The
README says so.

**D65. A stricter segment-level retrieval metric was added because the brief's
saturates.** All three modes put the correct source in the top three, so the
specified metric does not discriminate at this corpus size. A citation points
at a segment rather than at a meeting, so segment-level accuracy and the mean
rank of the cited segment are reported alongside it.

**D66. IndexFlatIP, exact, not approximate.**
The corpus is 148 segments. An approximate index exists to trade recall for
speed at a scale this is nowhere near, and using one would mean reporting
retrieval accuracy that is partly a property of the index rather than of the
embeddings.

**D67. A citation is verified against the source it cites, not against the
corpus.** A quote that appears in source 4 while the claim cites source 2 would
pass a corpus-wide check and produce a citation nobody can follow. The rubric
flags citations that point at a document rather than a location; one pointing
at the wrong location is worse, because it looks checkable.

**D68. An answer whose claims all fail verification is a not-found.**
Not a fluent paragraph with the citations quietly removed. The system would
rather say nothing than say something a reader cannot check, because a reader
cannot tell a real answer from an invented one and will believe both.

**D69. Verification runs twice.** Once as a validator inside the retry loop, so
a bad quote is repaired rather than rejected. Once again afterwards, so the
stored answer is verified by code that did not also produce it.

**D70. Only approved extractions are searchable.**
An unapproved extraction is a proposal a human has not accepted. Answering a
question from one would route around the approval gate.

**D71. A second embedder exists so the test suite does not load a transformer.**
`HashingEmbedder` produces genuine unit vectors with real cosine geometry, so
FAISS, the search and the fusion are exercised for real. It has no notion of
meaning, so no retrieval quality is ever asserted in the suite, and the harness
prints which embedder produced a comparison.

**D72. The interface shows each pipeline stage's real output.**
Source text, segments, chunks byte for byte, and the index. The brief says
chunking decides extraction quality and will be asked about, and the bytes are
worth more than a description of them.

**D73. The consent control on the upload form has no default.**
The form cannot be submitted until granted or withheld is chosen. Consent is a
property of the source and pre-ticking "granted" would quietly make the whole
guarantee meaningless. Uploading without consent is deliberately allowed,
because watching the refusal is worth more than reading about it.

### Known limitations

**L19.** Five golden questions cannot separate dense from hybrid. See D64.

**L20.** Retrieval is measured on 148 segments across three transcripts.
Nothing here says how any mode behaves at a scale where an approximate index
would be needed.

**L21.** The vector index is rebuilt in full rather than incrementally. Fine at
this size, and it would need deletion handling that nothing here exercises
before it could be otherwise.

**L22.** The React interface has no automated tests. A deliberate cut: the
brief awards no marks for the interface, and the review surface is exercised
through the API tests underneath it.

---

## Phase 7 — Chat signals

### Decisions

**D74. Chat does not go through the transcript pipeline.**
A transcript is a continuous conversation where meaning spans turns, so it is
chunked with overlap and a context header. A channel is a list of discrete
messages, each labelled on its own, with an id that must come back attached to
the right one. Forcing one shape onto both would have made both worse.
Messages are batched per channel in timestamp order, twenty at a time. Order
matters even though each is labelled separately, because "can you take a look?"
is unreadable without the message before it. Twenty at a time because
seventy-eight messages must not cost seventy-eight requests against a daily
allowance of twenty.

**D75. Direct messages are excluded by three independent mechanisms.**
The parser drops them, the schema refuses them via CHECK (is_direct_message = 0),
and the golden case checks the twelve forbidden ids against what actually
reached the store. Three, because this is the one property that cannot be
walked back: a private conversation that reaches the store has already reached
it.
A DM is identified by the export's flag OR by a channel name that looks like a
direct thread. Trusting only the flag would mean an export omitting it leaks
private conversation silently.

**D76. Noise is deleted, not stored with a label.**
The brief says discarded, not stored. The schema enforces it from the other
side: 'noise' is absent from the classification CHECK and could not be written
even deliberately.

**D77. Only decision, blocker and request are queued for review.**
Each could produce a downstream write, so each passes the same approval gate as
M3. A question is classified and kept but not queued, because answering one
writes nothing anywhere and queueing it would put work in front of a reviewer
with no downstream effect.

**D78. Precision, not accuracy, and the prompt is built around choosing noise.**
Every non-noise label becomes something a human must review, so labelling a
greeting as a request costs more than labelling a borderline request as noise.
The prompt says exactly that, and gives each of the four real labels the line
that separates it from the nearest thing it is not.

**D79. A message absent from the store counts as a prediction of noise.**
That is what its absence means. Getting this wrong would have quietly inflated
precision by dropping every noise prediction from the denominator.

**D80. Case 7b requires that messages were stored.**
Zero direct messages is trivially true of an empty store. The case fails with
"nothing is stored, so zero DMs proves nothing" when the store is empty, so the
zero cannot be earned by having done nothing.

**D81. Two model failure modes are repaired rather than tolerated.**
A dropped message triggers a retry, because a missing entry is not the same as
one labelled noise and the difference changes the precision figure. An invented
message id triggers a retry, because a label attached to the wrong message is
worse than no label and the mistake is otherwise invisible.

### Known limitations

**L23.** Precision is 0.87 on twenty hand-labelled messages. Twenty is a small
sample and the confidence interval around 0.87 is wide.

**L24.** The confusion is entirely between noise and request. A request is the
hardest of the five to define, since "can somebody look at this" and "someone
should look at this" differ only in whether anybody was actually asked.

---

## Phase 8 — The scheduler, digests and outcome records

### Decisions

**D82. A real scheduler, and its next fire times are read from it.**
APScheduler's BackgroundScheduler starts with the application, and
`/api/digests/schedule` reports each job's next run FROM the scheduler rather
than from configuration. A time read back from settings would prove only that
settings can be read. The brief calls a button with nothing behind it a partial
implementation, and a next-run timestamp that advances on its own is what
separates the two.

**D83. Two jobs, and the second is the one the rubric asked for.**
The digest job is the specified one. The expiry sweep is the answer to "a safe
default on timeout or no response": an unreviewed proposal ages out to a state
the approval-gate trigger treats exactly like pending, not writable. Nothing is
ever approved by the passage of time, and a test asserts the approved count is
unchanged after a sweep.
The anti-patterns tab asks that an agent do at least one thing without being
asked. Refusing to proceed on stale information is that thing.

**D84. Each digest item appears in exactly one section, attention first.**
Precedence: needs attention, then to decide, then moved. A blocker approved
today is both progress and a problem, and printed under both it fills two of
six lines with one fact, which is the opposite of what a fixed-size digest is
for. Found by rendering one and reading it.

**D85. The clock override is a parameter, not a demo path.**
`now` is threaded through building, emitting and the scheduled job, so what is
demonstrated is the same function that runs unattended rather than a parallel
one written for the walkthrough.

**D86. Posting a digest is not gated by approval.**
The task catalogue says posting is not an external write, and by the time a
digest exists every line in it came from something a human approved. A second
gate would ask a reviewer to approve their own earlier approvals.

**D87. Outcome records carry everything a consumer needs, and are versioned.**
Each item holds its own quote, speaker, timestamp and source rather than an
extraction id to look up, because the capability is that a second process can
reconstruct the approved items with no access to the transcript store. Records
are never overwritten: a consumer that read version 1 and acted on it should be
able to see what it read.

**D88. `docs/outcome_schema.json` is generated from the contract.**
A hand-written schema drifts from what is actually emitted, and a consumer
trusting the drifted version is worse off than one with no schema at all. A
test compares the published document against the live Pydantic schema. The
consumer contract at the top is hand-written, because it says what the fields
MEAN and JSON Schema cannot carry that.

**D89. A non-consented source gets no outcome record at all.**
An empty record would imply the source was handled. Refusing says it was not.

**D90. Three adapters, one per external system, each added when first called.**
Tracker in Phase 4, store and notifier here. None was written ahead of a
caller, because the anti-patterns tab treats a file named after an unbuilt
integration as implying work that does not exist.

**D91. The document store refuses a key that escapes its directory.**
Keys are derived from source ids, which arrive over HTTP. "../../etc/thing"
must not be writable, and the check belongs in the store rather than in every
caller.

### Known limitations

**L25.** The digest's selection rules are heuristics, not learned. "Needs
attention" means a high or medium risk, a blocker, or an action nobody owns.
They are stated on every line so a reader can disagree with the pick rather
than only the wording.

**L26.** The scheduler runs in-process. A second instance of the application
would run the jobs twice. Fine for a single-node review tool, and a real
deployment would need a lock or an external scheduler.

**L27.** Digests are per channel, and a meeting is treated as its own channel.
The specification says one digest per channel and a meeting has no other
natural grouping; doing otherwise would leave every transcript out of the
digest entirely.

---

## Phase 9 — Documentation

### Decisions

**The architecture note is written as one document a reviewer can read in ten
minutes, not as a folder of diagrams.** The brief asks for an architecture note
covering data flow, storage, gating and where the model sits. It is one file,
[`docs/architecture.md`](docs/architecture.md), with a single ASCII data-flow
diagram and tables under it. Rejected: generated diagrams. A picture of the
call graph would have said less than the four-row table stating where the
approval gate is enforced and what happens when each depth is bypassed.

**Each part of the system gets its own document, and each one follows the same
five headings.** What it does, which capability it serves, the decisions with
the reason that actually drove them, how it is tested, and what it does not do.
Twelve documents in [`docs/components/`](docs/components/README.md). The fixed
shape matters more than the prose: a reviewer comparing two components can find
the same thing in the same place, and a component with a thin "how it is
tested" section is visible at a glance.

**Every document ends with what it does not do.** The build's weak points are
recorded in the same file as its strengths rather than collected in one
appendix nobody reaches. The retrieval document says dense would win today. The
evaluation document says precision 0.71 is a lower bound because the golden set
is incomplete. Stating a limitation next to the decision it qualifies is the
only arrangement where the reader meets both.

**Bugs found during the build stay in the documents.** The `INSERT OR REPLACE`
cascade that destroyed fourteen extractions, the `.gitignore` line that
excluded every Pydantic contract, the quota-exhausted run that overwrote a good
results file. Each is written where its component is described, because each
one is the reason a check exists, and a check without its story reads as
ceremony.

**The component index carries a reading order, not just a list.** Someone with
twenty minutes should read the architecture note, the review gate and the
evaluation document, in that order, because those three carry the gating and
the numbers. The index says so.

### What writing the documentation found

**The README had gone stale in four places and nobody noticed.** The status
banner still said "Phase 2 of 12 complete". M8 was listed as Not built when
retrieval had been running and measured since Phase 6. M1 pointed at Phase 9
for audio transcription, which was never built. The file tree was missing
retrieval, the scheduler, the outcome record, the adapters and chat signals,
and the test count read 209 against an actual 328.

This is the same class of fault as the two wrong test counts in Phase 5: a
number stated from memory and never re-checked. The count is now taken from
`make test-inventory`. The capability table is not yet machine-checked, and
that is recorded below as `L28`.

**`make verify-clone` reported a dirty working tree, and the reason was a
`.gitignore` rule pointing at directories that do not exist.** The rules named
`data/digests/` and `data/outcome_records/`; the mock store writes under
`data/documents/`. Eleven generated files were tracked, so every run of the
scheduler left a diff nobody had authored. One of them was a transcript a user
had uploaded through the interface, committed to the repository as a side
effect of using the application. That is the part that mattered: files supplied
at runtime must not enter version control by accident.

This is the second fault caused by a `.gitignore` path being written without
thinking about what it matches. The first, in Phase 6, excluded every Pydantic
contract from git for nine phases. Both were found by a tool rather than by
reading, which is the argument for having built `make verify-clone` at all.

**Writing the evaluation document forced the matching rule to be stated in
prose.** Every recall and accuracy figure depends on it, and it had only ever
existed as code. Written out, it is plainly generous — quote overlap or a
half-share of content words — which is why recall and false positives are now
always printed together and neither is quoted alone.

### Known limitations

**L28.** The capability status table in the README is maintained by hand.
Nothing fails if it drifts from what the code does, and it had drifted twice.
The honest fix is a test that reads the table and asserts each Done row has a
passing test file behind it. Not built.

**L29.** The documentation describes the system at the end of Phase 9. It is
committed alongside the code and can go stale the same way the README did. No
process keeps them in step beyond rewriting both at the end of each phase.

**L30.** Audio ingestion is documented as not built rather than as future work.
`backend/app/audio/` holds an empty package. M1 stays Partial.

---

## Phase 10 — The follow-up draft and the per-person digest

Both are COULD capabilities, built after the SHOULDs were finished and
measured, on the basis that a capability nobody expects is worth more built than
a capability everybody expects built twice.

### Decisions

**The recap is rendered from approved rows, not written by the model.** This is
the decision to defend. A model asked to summarise approved items produces
sentences nobody approved: the reviewer approved a task description and a quote,
and a paraphrase of five of those is new text that passed no gate. A template
produces exactly the approved text with the quote attached. Rejected: a model
pass over the rendered draft. It would work with a check on every sentence
against an approved quote, but the check is the hard half, the plain version is
already correct, and it costs a model call against a twenty-a-day allowance.

**Sending is gated. Drafting is not.** Drafting is not an external write:
nothing leaves the machine and every line already passed the approval gate. A
gate there would ask a reviewer to approve their own earlier approvals, the same
argument the digest makes. Sending is the only thing in this build a person
triggers by hand every single time.

**`sent_by` is the capability, so it is refused four ways.** Blank and
service-named senders are refused in `followup/draft.py` for a readable message,
and again by `trg_followup_send_requires_person` and
`trg_followup_agent_cannot_send` when the service layer is bypassed. The
endpoint has no default for the field: a default would be the agent sending
under whatever name the default carried, and a test asserts a request omitting
it fails validation. `trg_followup_insert_is_draft` closes the way round, since
an INSERT arriving already marked sent would walk past every rule written on
UPDATE.

**One test is structural rather than behavioural.** No file under
`app/scheduler` mentions follow-ups at all. A scheduled job that sent a recap
would satisfy every behavioural test in the file and break the one rule M12
states, so the test is about what the code does not contain.

**The generated recap and the human's edit are stored side by side.** Same split
as `original_payload` on extractions, same reason: which half a reader is
looking at is the first question worth asking about a machine-drafted message.
`trg_followup_generated_body_immutable` makes the answer unforgeable, and
`trg_followup_sent_is_final` stops a sent message being rewritten afterwards to
say something the sender did not send.

**There is no recap of nothing.** A source with no approved items raises rather
than producing an empty message, because an empty recap sent by mistake states
that the meeting produced nothing, which is a claim the system has no basis for.

**People are grouped by their first name, casefolded.** Instructed, and
defensible for the common case: the owner of an action is free text lifted from
a transcript, so one person is "Priya" in one line and "Priya Sharma" in
another, and grouping on the raw string produces two digests neither of which is
a per-person view of anything. The cost is that Priya Sharma and Priya Menon
share a digest, and the sprint planning transcript contains exactly that pair on
purpose.

The cost is paid back in evidence rather than hidden. Every line carries the
owner string exactly as the transcript gave it, a grouped person reports itself
as ambiguous, the digest text names the full names it covers, and the interface
badges the person and lists the aliases. Rejected: splitting on the full name.
It is worse in the common case, because the transcript usually says "Priya" and
the split leaves the person reading two partial digests.
`PERSON_IDENTITY=full_name` switches to the strict rule and both are tested.

**A bare first name takes its full name from the participant list when exactly
one participant matches.** That is a lookup in metadata the meeting supplied,
not a guess. With two candidates it declines and keeps the bare first name,
which is the same abstention discipline the extraction uses.

**Unowned work gets its own digest and says so.** Dropping it hides real work
and assigning it invents an owner, so everything with `owner = UNSPECIFIED`
collapses into one bucket headed "Assignee unspecified" where every line states
the task and then says the assignee is unspecified. No placeholder is ever
promoted into a display name, and a test asserts no unowned item reaches a named
person's digest.

**The person digest reuses the M10 machinery and none of its shape.** It is
cross-source, because a commitment is a commitment whichever meeting it was made
in. It is uncapped, because the 3/2/1 shape exists to force a choice across a
whole channel and capping somebody's own commitments would drop the fourth one
silently. Somebody with nothing approved gets no digest at all, enforced where
digests are emitted rather than by rendering an empty file.

**Person digests are written and not posted.** A channel digest belongs in its
channel. One person's workload posted into a shared channel is a different thing
from the digest they asked for. `POST_PERSON_DIGESTS` turns it on for a
demonstration and a test asserts the default is off.

### What building this found

**The test fixture redirected seven of the nine writable paths.**
`notification_log_path` and `document_store_dir` were missing, so every test
touching the notifier or the document store read and wrote the real files under
`write_log/` and `data/documents/`. It surfaced only when a new test asserted
that no post had been made and saw 41 left by earlier manual runs. Existing
tests never caught it because they assert a post exists, which stays true
whatever else is in the log. It also explains the generated files that kept
appearing in `git status`: the suite was writing them.

The general form is worth keeping. A test asserting something is present passes
in a dirty environment, and only a test asserting something is absent finds the
leak.

**The health test pinned `schema_version` to the literal "1".** It broke the
moment the schema gained a table, which is the test doing its job, but the
assertion was on the number rather than on the reporting. It now compares
against what the store reports.

**The architecture note's component line counts did not match the source, by a
wide margin, and the counting method was never recorded.** There was no way to
tell what the old numbers had measured, so they were recounted and the command
is now printed under the table. The coverage table in `docs/testing.md` had
drifted the same way: five files missing outright and one count wrong by five.
It is generated from `make test-inventory` now.

### Known limitations

**L31.** Name matching handles first names, full names and titles. It does not
handle nicknames, initials or email addresses. Each needs a source of truth this
build does not have, and guessing that Bob is Robert is exactly the kind of
inference the rest of the system refuses to make.

**L32.** The recap has no model polish pass. It would be a real improvement in
readability and it is not built, because every sentence would need checking
against an approved quote before display and the check is the harder half.

**L33.** A person digest is written to the document store and not delivered to
that person. Delivery is the notifier's problem, and posting personal workloads
into a shared channel is worse than not posting them.

**L34.** The recap covers one source. A weekly recap across several meetings
would need a different selection rule, and inventing one without a user to ask
would be guessing at the requirement.

---

## Phase 11 — Uploading a chat export

### Decisions

**One endpoint for both kinds of source.** A transcript and a chat export
differ only in which parser reads the bytes. The consent check on the metadata
before the file is opened, the refusal leaving nothing on disk, and the report
shape coming back are identical. Rejected: a second endpoint at
`/api/sources/upload-chat`. It would have been a second copy of the consent
gate, and the gate is the one thing in this application that must exist exactly
once. Two copies of a rule are two chances for them to disagree.

**The kind is chosen by the person uploading, not sniffed from the file.** The
selector is the first control on the form. A `.json` file is a perfectly good
transcript, so the extension does not settle it, and guessing wrong would route
private channel messages through the transcript parser. Rejected: inspecting
the JSON for a `messages` key. It works for the sample export and fails for the
next one, and a wrong guess here has a privacy cost rather than a formatting
cost.

**`audio` is refused by name.** `POST /api/sources/upload` with
`source_type=audio` returns 422 saying audio ingestion is not built. Letting it
fall through to the transcript parser would report a parse failure for a file
nothing here can read, which describes the wrong problem and would make M1 look
broken rather than incomplete.

**`source_type` defaults to `transcript`, and `consent_flag` still has no
default.** The asymmetry is deliberate. A wrong default for the kind costs a
readable parse error. A default for consent would quietly make the whole gate
meaningless. Defaults are acceptable where the cost of being wrong is an error
message and unacceptable where it is a guarantee.

**The interface shows the count of direct messages dropped.** In amber, beside
the message count, in the upload result. The messages themselves leave no
trace, so the count is the only evidence they were ever seen, and "zero DM
records in the store" is otherwise indistinguishable from "the export had
none".

### What building this found

**The gap itself was found by using the system, not by reading it.** The upload
endpoint had hardcoded `SourceType.TRANSCRIPT` since Phase 1 and every test
passed, because every test uploaded a transcript. M9 was marked Done and
measured, and the only way to exercise it on a new export was to edit a file in
`sample_data/` and re-seed. A capability reachable only by editing the
repository is not reachable.

Verified end to end against the running API rather than only in tests: the
committed sample export uploaded through HTTP gives 78 messages stored, 12
direct messages dropped and 0 direct messages in the database.

### Known limitations

**L35.** Only one export format is accepted, the flat `{"messages": [...]}`
shape the sample uses. The parser reads several key spellings for each field,
so a Slack or Teams export would need a converter rather than a rewrite, but
neither is built or tested.

**L36.** Uploading an export with an id already in the store replaces its
messages. That follows `replace_messages`, which is right for re-ingesting the
same export and wrong if two different exports are given the same id by
accident. Nothing warns about it.

---

## Phase 11a — A message id is only unique inside its own export

### What happened

Uploading a second chat export returned **500**, `sqlite3.IntegrityError: UNIQUE
constraint failed: chat_messages.id`. The first export in the store had messages
`msg_001` upward. So did the second. `chat_messages.id` was the export's own
identifier used as a global primary key.

Two things were wrong, and the second is worse than the first. Every export tool
numbers its messages from one, so a collision is the normal case rather than a
corner case. And it surfaced as an unhandled 500 rather than as a stated
refusal, which is the failure mode this build is supposed to not have.

### Decisions

**The fix is the convention the codebase already had.** Segments have been
stored as `source_id::seg0000` since Phase 1, for exactly this reason: the
identifier is unique inside its own source and nowhere else. Chat messages are
now `source_id::msg_001`. Rejected: a composite primary key of
`(source_id, id)`. It would leave `extractions.message_id` ambiguous on its own,
and every join would need to carry the source alongside it.

**The export's own id is kept, in `external_id`.** Namespacing without keeping
it would lose the only handle back to the system the message came from. A
`UNIQUE (source_id, external_id)` constraint keeps one export from containing
the same id twice.

**Three places had to choose which id they meant.** The model is shown the
export's id, because it is short and it is the one a person reading the channel
would recognise, and the mapping back to the stored row happens where the batch
is already in hand. `extractions.message_id` holds the stored key, because it is
a reference. The payload holds the export's id, because it is read by people. A
test asserts both, since they are two different jobs.

**The golden labels were not touched.** The harness keys stored messages by
`external_id` instead. Correcting hand-written ground truth to accommodate a
schema change would be the wrong way round, and the same rule applied in Phase 5
when a golden label was genuinely wrong: labels change only when the source
says so.

### What this says about the tests

Nothing in 410 tests caught it, and the reason is worth writing down. Every test
ingested **one** chat export, because the sample data contains one. The property
"two sources can coexist" was never expressed, so the schema was free to assume
one. Both regression tests now state it directly: two exports numbering from
`msg_001` store in full, and re-uploading one leaves the other alone.

This is the third fault of the same shape in this build, after the `.gitignore`
rules and the test fixture writing to real paths. In each case the code was
correct for the single case the fixtures exercised, and wrong for the second one
nobody had.

### Known limitations

**L37.** `schema_version` moves to 3 and there are still no migrations. An
existing database has to be rebuilt with `make seed` or `make seed-empty`. Fine
for a review tool where the store is a build artefact, and stated rather than
hidden.


---

## Phase 11d — The demo button and the scheduled job had drifted apart

**What happened.** `POST /api/digests/run/all` carried the docstring *"exactly
what the scheduler runs at the configured hour"* and called `emit_all`, which
writes channel digests only. Since Phase 10 the scheduled job has written
channel digests **and then** person digests. The claim had been false for two
phases, and the button in the interface demonstrated a path nothing runs
unattended.

**Why it matters more than it looks.** The rubric's stated red flag is a button
with no scheduler behind it. This is the quieter version: a button behind a real
function, but not the same one. The obvious version is visible in the code. This
one is only visible by reading two files side by side and noticing they disagree.

**The fix.** One function, `jobs.run_end_of_day`, returning what it wrote as
`{channels, people}`. The scheduler calls it with trigger `scheduler`. The
endpoint calls it with an injectable clock and a different label. Nothing else
differs, so the claim is true again by construction rather than by discipline.

**The test states the property, not the call.** The endpoint's response and the
`digests` table have to agree on both kinds. An endpoint narrowing again fails
it, which the previous test could not do because it only counted channels.

**How it was found.** By being asked what the button does, and reading the code
to answer rather than answering from memory. Two of the last four faults in this
build surfaced the same way: not by a test, and not by using the interface, but
by having to explain a claim precisely enough to check it.

**L38.** The same drift is possible anywhere a demo control and a scheduled path
are wired separately. Nothing structural prevents it. The guard here is one
shared function and one test, applied to this pair only.

---

## Phase 12 — Audio ingestion, and the signal class in the queue

### Decisions

**The transcriber is a parser, not a pipeline.** It returns `RawSegment`s
exactly as the txt, vtt and json parsers do, so validation, normalisation,
character offsets and quote verification are the same code afterwards. Audio
adds an input rather than a second path. `TranscriptFormat.AUDIO` had existed
since Phase 1, so the shape was already there.

**Whisper runs in a subprocess, and the reason is measured rather than
stylistic.** faiss and ctranslate2 each link their own OpenMP runtime. A faiss
search followed by a whisper load in one process aborts on this machine with
`OMP: Error #15`, and the API loads faiss for retrieval. Rejected:
`KMP_DUPLICATE_LIB_OK=TRUE`, which the runtime itself describes as unsafe and
capable of silently producing incorrect results. Silently incorrect is the
failure mode this build refuses everywhere else, and taking the workaround to
make a demo run would be choosing a wrong answer over a slow one. The worker
costs a few seconds of startup for a file uploaded by hand, and a decoder
failure now kills a worker rather than the API.

**`available()` probes with `find_spec` rather than importing.** The first
version imported `faster_whisper` to answer the question, which loaded
ctranslate2 and PyAV, which aborted the whole test suite the moment a later
test touched faiss. An availability check has no business initialising two
native runtimes.

**Nobody is attributed, ever.** Whisper returns words and timings, not
speakers. Every segment carries `speaker = None` and every named participant is
reported as silent, which is accurate: nothing knows who spoke. Rejected:
inferring the speaker from the participant list or from the previous line. Rule
one of the brief forbids inventing a speaker, and a recording is exactly where
that temptation is strongest.

**Every audio source carries a warning naming the model.** Transcribing a test
clip turned "Nuwan" into "new one" and "I am blocked" into "I unblocked". A
verbatim quote from a recording is faithful to the transcript and not
necessarily to the room, so the distinction is written onto the record rather
than left for a reviewer to work out.

**Silence is an error, not an empty success.** A recording producing no
segments reports an error. An empty transcript read as success would report a
meeting in which nothing was said, which is a claim the system cannot support.

**A worker that prints nothing is a failure, not an empty transcript.** A
worker killed by the OS produces no stdout, and reading that as "no speech"
would turn a crash into a silent meeting. Tested directly.

**The review queue badges a signal with its class, not with the word Signal.**
Twelve chat items all badged "Signal" tells a reviewer nothing, and the class
was in muted grey between the channel and the author. For an action, a decision
or a risk the type is the whole story. For a signal the type is the container
and the class is the story, so `KindView` gained an optional badge and only the
signal kind uses it.

### Known limitations

**L39.** No diarisation. A recording produces unattributed segments, so actions
extracted from audio will mostly carry `owner: UNSPECIFIED` unless a name is
spoken aloud. Adding it means a second model and a second measurement, and
guessing without one is worse than abstaining.

**L40.** Transcription quality is not measured. There is no golden audio file
and no word error rate, so the harness says nothing about how well the words
were heard. The evidence a reviewer has is the warning on the record and the
audio itself.

**L41.** The worker is spawned per file with no queue and no cancellation. A
long recording holds a request open for minutes. Fine for a review tool where
somebody uploads one file at a time, and wrong for anything concurrent.

---

## Phase 13 — A tool-dispatch loop, on LangGraph

The catalogue asks for *"multi-step tool use, if you need it"* and offers two
routes: plain functions plus a dispatch loop, recommended for a build this size,
or a framework if you already know one.

### Decisions

**A framework, and it has to earn the choice.** The loop itself is four lines
either way, so LangGraph is not justified by the loop. It is justified by three
things it gives as data rather than as code I would otherwise write and test.
The state is a value, so every message, observation and step count comes back in
one object and the trace shown to a reviewer is what executed rather than a log
written beside it. The budget sits on an edge, enforced where it is visible
rather than inside a condition somebody can forget. And `@tool` derives the JSON
schema from the docstring, so the description a model reads and the function a
developer reads are the same text. Rejected: a hand-written while loop. It would
have been smaller and I would have had to build the trace and the budget check
myself, which is the part with the bugs in it.

**No tool crosses a gate, and that is the architecture.** Nine tools read, one
writes and it writes `pending`, and there is no tool for approving, writing
outward or sending. An agent able to approve its own proposals makes the
approval gate reachable by a model deciding it was confident, which would undo
the property the rest of the build is organised around. The catalogue asks for
multi-step tool use, not for autonomy over the gate.

**The guard is structural, not a naming convention.** One test checks the tool
names, and a second reads the module source and asserts it imports neither the
tracker service nor the follow-up sender and never calls `queue.approve`. A tool
crossing a gate could be added tomorrow by somebody who did not read this entry,
and the name check alone would not catch a tool called `finalise_item`.

**`propose_action_item` is held to the evidence standard the chunk reader is
held to.** The quote must be a literal substring of the source or the proposal
is refused and nothing is stored. An agent allowed to propose on its own
recollection would fill the queue with items no reviewer could check, which is
worse than an agent with no write at all.

**Every planning call is recorded.** A callback writes one `llm_calls` row per
call with capability `agent`. Without it a run would spend requests invisibly
and the usage panel would understate what the loop cost, turning a cost claim
into a guess.

**The endpoint caps the budget at 20.** A caller asking for five thousand steps
gets twenty. An endpoint accepting an unbounded loop is an endpoint that can be
asked to spend a whole day's quota in one request.

**The planner is scripted in every test.** A test asserting the model chose the
right tool would be measuring the model, and that belongs in the harness.

### What the first real run against Gemini found

Every test passed against the stub. Then the loop met the real planner and
produced three faults in one run.

**The answer arrived as a JSON blob.** Gemini returns content as a list of parts
rather than a string. The stub returns strings, so nothing caught it.

**The loop spent its whole budget browsing**: five steps, four of them
single-word searches, no answer. The planner had no way to see its own budget.
Observations now carry `[step 2 of 4, 2 left]`.

**And the answer was wrong.** Asked which meetings mention the staging
environment being down, it said none did. `msg_003` in the stored chat export
says exactly that, raised by Marcus Webb. The cause was a gap between two things
that both look like search: `search_transcripts` covers segments and approved
extractions and cannot see chat, while `read_chat_messages` takes filters rather
than a query and its first twenty rows were all from the other channel.
`chat_messages_fts` had existed since Phase 7 and nothing searched it.

The third is the one worth keeping. The answer was confident, quoted real text,
and came from a system built around verifiable output. Nothing in 466 tests
could have caught it, because it was not a code fault: it was a hole in what the
agent could reach. Reading the answer against data whose contents were already
known is what caught it, which is the argument for demonstrating on a corpus you
know by heart.

### Known limitations

**L39.** Three dependencies and a second path to the model. Extraction calls go
through `call_structured` with its cache, retry policy and per-attempt
telemetry. The agent's planning calls go through LangChain instead, and only
telemetry is shared. Two paths to one provider is a real cost of the framework.

**L40.** The closing prose is verified by nobody. Individual observations are
evidence and `answer_with_citations` verifies its own quotes, but the paragraph
the loop writes at the end is not checked against them. A verifier over the
final answer is the obvious next step and is not built.

**L41.** The loop is demonstrated, not measured. Scoring it needs a golden set
of instructions with expected tool sequences and acceptable answers, which does
not exist. Every number in `eval/results.txt` is about extraction, retrieval and
classification.

**L42.** No memory between runs and no parallel tool calls. Each instruction
starts clean, and tools requested in one turn run in order.

---

## Phase 14 — Containerised

The build spec lists Docker and docker-compose for packaging. The claim being
made is narrow and worth stating: **a reviewer with Docker and a Gemini key
needs nothing else installed.** No Python version, no Node, no faiss wheel, no
model weights.

### Decisions

**CPU-only torch, installed explicitly and before everything else.**
`sentence-transformers` pulls torch, and on Linux the default index serves the
CUDA build: roughly 2.5GB of NVIDIA libraries for a container with no GPU.
Installing the CPU wheel from PyTorch's own index first leaves the resolver with
torch already satisfied when it reaches sentence-transformers. Rejected: letting
pip resolve it. The image would work and be four times the size, and nobody
would notice until the first push.

**The embedding model is baked into the image, whisper's is not.** Retrieval is
the default path, so a container downloading 90MB on its first question is a
container that fails behind a corporate proxy or on a plane. Audio is optional
and its model is 140MB, so it caches to a volume on first use instead. The split
is a judgement about which failure is more likely to be seen by a reviewer.

**Two stages, so the compiler never ships.** `build-essential` is needed to
install and useless to run. Keeping it would add about 200MB of tooling to the
image that serves HTTP.

**The interface is static files behind nginx, not a dev server.** `vite dev` in
a container ships the whole toolchain to serve a page. nginx also proxies `/api`
and `/health`, so the browser talks to one origin, `CORS_ORIGINS` is empty in
the container rather than widened, and what a reviewer sees behind nginx matches
what a developer sees behind Vite.

**The healthcheck waits on `/health` answering, not on the process existing.**
compose then holds the interface back until the schema is applied. A UI loading
first shows empty panels and reads as broken.

**`.env` is excluded from the build context entirely.** A key baked into a layer
is a key that cannot be rotated and travels with the image. It arrives through
`env_file` at run time.

**Only the store and the logs are on volumes.** Everything else is rebuilt from
the image, which is the whole basis of the reproducibility claim: code and
dependencies come from the build, and data is the only thing that survives.

**`make docker-test` runs the suite inside the image.** `make verify-clone`
proves the repository holds everything. Running the tests in the container
proves the dependencies do, which is a different claim and the one a reviewer on
another machine cares about.

### Known limitations

**L43.** ~~Neither image has been built.~~ **Resolved.** Both images built on
2026-08-24 on arm64, and the first run found three faults. Recorded below.
Neither image has been built for `linux/amd64`, which is still untested.

**L44.** No image is pinned by digest and no lock file covers the Python side.
`pip install -r requirements.txt` resolves transitive dependencies at build
time, so two builds a month apart can differ. The direct pins are exact, which
is what determines behaviour, and a full lock file is the honest fix.

**L45.** One process per service and no orchestration beyond compose. The
scheduler runs in the API container, so scaling the API to two replicas would
run every job twice. The same limitation the local build has, now with a
tempting way to trip over it.


---

## Phase 14a — What the first container build found

Three faults in one afternoon, and none of them was reachable by any test on a
machine that already worked.

**The API would not start: `ModuleNotFoundError: No module named 'apscheduler'`.**
`app/scheduler/jobs.py` has imported it since Phase 8 and it was never declared
in `requirements.txt`. It was present locally as a transitive dependency of
something else, so the application started on every machine where pip had
already run for another reason, and died on the first machine where it had not.

`make verify-clone` could not have caught it, and the distinction is worth being
precise about. verify-clone clones the repository into a temporary directory and
runs the suite **in the same virtualenv**. It proves the repository holds every
file. It says nothing about whether the dependency list is complete. A container
build is the only thing here that starts from nothing.

An audit of every third-party import across `backend/`, `eval/` and `scripts/`
against `requirements.txt` found no other omission.

**The suite failed inside the image on two tests reading
`docs/outcome_schema.json`**, which `.dockerignore` excluded along with the rest
of `docs/`. That file is not documentation: it is the published contract a
downstream consumer reads, and the two tests assert the published file still
matches the Pydantic model. It is now the single exception in the ignore rules,
with the reason written beside it. The container did not break anything here, it
revealed a file filed under the wrong idea.

**The interface reported itself unhealthy while serving every request
correctly.** Inside the container `localhost` resolves to `::1` first, nginx
listens on IPv4 only, and busybox `wget` does not fall back to `127.0.0.1` the
way curl does. So the healthcheck got connection refused from a server answering
200 on every path. The check now names `127.0.0.1`. Rejected: adding
`listen [::]:80` to nginx, which fixes a client-side fault by widening what the
server binds.

**What the three have in common.** Each was invisible on a machine that had
already been made to work, and each surfaced the first time the system was
built from nothing on a clean base. That is the argument for containerising a
project that already ran fine.

### Verified in the container

    make docker-build     both images, 2.7GB api and 76MB ui
    make docker-up        api healthy, ui healthy
    make docker-seed      6 sources, 1 refused, 1 malformed, as expected
    make docker-test      466 tests, all passing inside the image

`curl localhost:5173/health` and `curl localhost:5173/api/agent/tools` both
answer through nginx, so the one-origin claim holds.
