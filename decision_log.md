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
