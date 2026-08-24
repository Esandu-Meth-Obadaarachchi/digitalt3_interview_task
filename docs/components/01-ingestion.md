# 01 · Ingestion and the consent gate

**Capabilities:** M1 (ingest and normalise a source), M2 (consent gate)
**Code:** `backend/app/ingestion/`, `backend/app/audio/`
**Tests:** 97 across `test_audio_ingestion.py` (34), `test_consent_gate.py` (8), `test_transcript_parsers.py` (18),
`test_ingestion_pipeline.py` (20), `test_api_sources.py` (18)

---

## What it does

```
metadata ──► CONSENT GATE ──► read ──► detect format ──► parse
                  │                                        │
            refused: stop                                  ▼
            bytes_read: 0                            validate defects
                                                           │
                                    ┌──────────────────────┴──────────┐
                                    │                                 │
                              blocking defect                    no blocking
                                    │                                 │
                              status = error                   normalise
                              nothing stored                          │
                                                            segments + offsets
```

Three outcomes, all recorded:

| Outcome | Meaning | Evidence |
|---|---|---|
| `ingested` | Segments stored, warnings travel with the source | segment count |
| `refused` | Consent withheld, nothing read | **`bytes_read: 0`** |
| `error` | Blocking defect, nothing stored beyond the reason | `error_detail` |

---

## M2 — the consent gate

The capability test: the meeting with `consent=false` is *"never transcribed,
never sent to a model, and produces zero extracted items"*.

**The gate fires on metadata, before the file is opened.** Checking after
parsing would satisfy the wording while the content sat in memory and in the
store.

The evidence is machine-checkable rather than asserted: a refused source's
report carries `bytes_read: 0` and `content_hash: null`. During the demo that
number is the proof.

**Three layers:**

1. `consent.py` — on metadata, before any file access
2. `extraction/pipeline.py` — before any model call
3. `trg_consent_gate_insert` — an extraction cannot exist for a non-consented source

`SourceMetadata.consent_flag` has **no default**. A source omitting it fails
validation, because absent consent is not consent and a default in either
direction would be a decision the source never made.

---

## M1 — parsing

Three formats, one internal shape.

| Format | Parser | Convention handled |
|---|---|---|
| txt | `parsers/txt.py` | `[00:00:05] Speaker: text`, and lines missing either part |
| vtt | `parsers/vtt.py` | `<v Speaker>` voice tags **and** `Speaker: text` prefixes |
| json | `parsers/json_transcript.py` | Alternative key spellings from different STT tools |

**Format is detected from content, not the extension.** A VTT file renamed
`.txt` is still parsed as VTT — parsing it as prose would lose every cue.

`test_all_three_formats_normalise_to_identical_segments` parses the same
conversation from all three and asserts 51 segments each with identical speaker,
text and timestamp. That is what makes the format-agnostic claim checkable
rather than asserted.

### A bug this caught

`detect_format` originally sniffed content starting with `[` as a JSON array. A
plain transcript opens on `[00:00:05]`, so **every `.txt` transcript was routed
to the JSON parser** and rejected as invalid JSON. Found by running the parsers
against the real sample data rather than against fixtures.
`test_a_transcript_opening_on_a_timestamp_is_not_mistaken_for_json` pins it.

---

## Speaker attribution, and refusing to guess

Naively splitting on the first colon turns `"Note: the deadline moved"` into a
speaker called Note. That is an invented speaker, which rule 1 of the brief
forbids outright.

**Two signals, in order of confidence:**

1. The candidate matches a participant named in the source metadata
2. The candidate is name-shaped: **two to four** capitalised tokens, no sentence
   punctuation, first word not a document-structure word

A **single** capitalised word is accepted only on signal 1. On its own it is too
weak.

| Input | Result |
|---|---|
| `Sarah Chen: hello` | `Sarah Chen` |
| `Sarah: hello` *(metadata names her)* | `Sarah Chen` |
| `Sarah: hello` *(no metadata)* | **unattributed** — the safe failure |
| `Note: the deadline moved` | not a speaker |
| `Action Items: three of them` | not a speaker |
| `https://example.com/x: see this` | not a speaker |

**A first name shared by two participants is never resolved.** The sample data
plants Priya Sharma and Priya Menon in one meeting, so a bare "Priya" is
ambiguous and picking one would be a guess.

**An unlabelled line keeps `speaker: null`** and records a warning saying why.
In `malformed_meeting.txt` three lines carry a timestamp but no speaker, and
each directly follows a line by the same person — inheriting would look correct
almost every time and would still be a guess.

---

## Severity-graded defects

The brief requires the deliberately malformed sample be *"rejected with a clear
reason"* and must not *"corrupt the store"*. It does **not** require rejecting
every imperfect file, and a transcript with one unlabelled line is realistic
input.

| Blocking → nothing stored | Non-blocking → travels with the source |
|---|---|
| `truncated_mid_sentence` | `missing_speaker_label` |
| `undecodable_bytes` | `missing_timestamp` |
| `no_parseable_segments` | `non_monotonic_timestamp` |
| `unknown_format` | `empty_segment_text` |
| `malformed_structure`, `empty_file` | `replacement_characters` |

**Truncation detection is a heuristic and is documented as one.** The rule: the
last segment does not end in terminal punctuation. On the committed samples this
cleanly separates `malformed_meeting.txt` (ends on `"upd"`) from the three valid
transcripts. The false-positive mode is named and the check is switchable per
source, which is tested.

**Decoding is strict UTF-8.** A lenient decode puts U+FFFD into stored text and
from there into a quote the system would present as verbatim. The defect names
the byte, its offset and its line, and carries a readable excerpt so a human
agrees with the rejection rather than taking it on trust.

**The rejection message names the most diagnostic defect, not the first.** A
short malformed upload tripped two checks at once and reported *"only 1 segment
parsed, usually the wrong parser"* when the real problem was truncation. Blocking
defects are now ranked, with `no_parseable_segments` last as the vaguest.

---

## Audio, and what a machine transcript is evidence of

The transcriber is **a parser, not a pipeline**. It returns `RawSegment`s
exactly as the txt, vtt and json parsers do, so validation, normalisation,
character offsets and quote verification are all the same code afterwards.

Two properties of a machine transcript are recorded rather than smoothed over,
because each changes what a quote drawn from it means.

**Nobody is attributed.** Whisper returns words and timings, not speakers.
Diarisation is out of scope in the brief, so every segment carries
`speaker = None` and every named participant is reported as **silent** — not
because they said nothing, but because nothing knows who spoke. Filling it in
from the participant list, or from the previous line, would be inventing an
attribution. The practical consequence is real: an owner can only be extracted
where somebody is named aloud in the words.

**The words are a guess.** Transcribing a test clip on this machine turned
*"Nuwan"* into *"new one"* and *"I am blocked"* into *"I unblocked"*. So every
audio source carries a warning naming the model:

> machine transcription by whisper:base: the words are the model's best guess
> and no speaker labels exist. Every segment is unattributed, so an owner can
> only be extracted where somebody is named aloud.

A verbatim quote from a recording is faithful to **the transcript**, not
necessarily to the room. The warning is what keeps that distinction visible to
a reviewer.

### It runs in a subprocess, and that is a decision

`faiss` and `ctranslate2` each link their own OpenMP runtime. Loading both into
one process aborts on macOS:

```
OMP: Error #15: Initializing libomp.dylib, but found libomp.dylib
already initialized.
```

The API loads faiss for retrieval, so it cannot also load whisper. Measured
here: a faiss search followed by a whisper load in one process kills the
process.

The documented workaround is `KMP_DUPLICATE_LIB_OK=TRUE`, which the runtime
itself calls unsafe and capable of **silently producing incorrect results**.
Silently incorrect is the one failure mode this build refuses everywhere else,
so it is refused here. `whisper_worker.py` runs standalone, imports nothing
from `app`, and prints JSON. A few seconds of startup buys correctness, and a
decoder failure now kills a worker rather than the API.

`available()` probes with `find_spec` rather than importing, for the same
reason. The first version imported, and aborted the test suite.

### Four refusals, each naming the real problem

| Situation | Result |
|---|---|
| Extension is not audio | error **before any decoder sees it** |
| File missing | error naming the path |
| `WHISPER_ENABLED=false` | error saying it is switched off |
| Decoder failed | error carrying the decoder's own message |

Silence is an error too. An empty transcript read as success would report a
meeting in which nothing was said.

---

## Two ways in, one gate

| Route | Used by | Declares consent |
|---|---|---|
| Manifest | `make seed`, the harness, the test suite | `sample_data/metadata/sources.json` |
| Upload | `POST /api/sources/upload` | a required form field |

Both kinds of source take the **same endpoint**. A transcript and a chat export
differ only in which parser reads the bytes: the consent check on the metadata
before the file is opened, the refusal leaving nothing on disk, and the report
coming back are identical. A second endpoint would mean **a second copy of the
gate**, and the gate is the one thing here that must exist exactly once.

`source_type` is a form field with three accepted values: `transcript`,
`audio` and `chat_export`. Three details are deliberate:

**The kind is chosen, not sniffed.** The interface asks before the upload rather
than inferring from the extension. A `.json` file is a perfectly good
transcript, and guessing wrong would route private channel messages through the
transcript parser.

**An unknown kind is refused by name.** Falling through to the transcript
parser would report a parse failure for a file nothing here can read, which
describes the wrong problem.

**The field defaults to `transcript`.** Unlike `consent_flag`, which has no
default at all, a wrong default here costs a readable parse error rather than a
privacy failure, and callers written before the field keep working.

For an export, the interface shows the **count of direct messages dropped** in
the result panel. That count is the only trace a direct message was ever seen,
so it belongs on screen rather than in a report nobody opens. Uploading the
committed sample export gives 78 messages stored, 12 dropped, none in the
database.

---

## One definition of "the text of a source"

```
source_text = " ".join(whitespace-normalised segment texts, in order)
```

Quote verification checks against that string, and every `char_start` /
`char_end` indexes into it. Because both use one definition, an offset is
checkable by hand and a quote spanning two segments still verifies.

**Speaker labels and timestamps are excluded deliberately.** A verbatim quote
must be words somebody said, not `"[00:02:17] Priya Sharma:"`.

`test_every_golden_quote_is_a_substring_of_the_normalised_source_text` asserts
every hand-labelled quote verifies against this exact string, so ground truth
cannot drift from the system measured against it.

Exposed at `GET /api/sources/{id}/text` so a reviewer can check any citation by
hand.

---

## The data-loss bug this component had

Pressing "Seed sample data" with 17 extractions in the queue left 3.

`upsert_source` used `INSERT OR REPLACE`. SQLite implements that as **delete
then insert**, and the delete fired `ON DELETE CASCADE` against `extractions`.

| Extraction state | What happened |
|---|---|
| pending, never reviewed | **silently destroyed** |
| edited / approved / rejected | cascade hit `review_events`, whose append-only trigger refused, so the whole ingest failed loudly |

The Phase 0 audit trigger is the only reason reviewed work survived. It was
written to stop tampering and it caught an accident.

**Fixed two ways:** `upsert_source` now UPDATEs in place, and re-ingesting
content whose hash is unchanged is a genuine no-op — because rewriting segments
was nulling `extraction.segment_id` through `ON DELETE SET NULL` and orphaning
perfectly good citations. Five regression tests.

---

## How it is tested

| What | Tests |
|---|---|
| M2 consent, including `bytes_read: 0` and the DB backstop | 8 |
| Format detection, three-way equivalence, speaker heuristic, read defects | 18 |
| Defect grading, offsets, end-to-end outcomes, re-ingestion safety | 20 |

Tests assert **properties, not sample-set totals**. Three once hard-coded "106
segments" and broke when a fifth transcript arrived, for a reason unrelated to
what they tested. They now assert that every ingested source stores exactly the
segments its report claims, every refused or errored source stores none, and the
FTS index matches the segment count whatever it is.

---

## What it does not do

- **Speaker attribution for audio.** Diarisation is out of scope in the brief,
  so a recording produces unattributed segments and nothing guesses. `L39`.
- **Speaker diarisation** — out of scope per the brief. Labels come from the
  source or are absent; nothing infers who spoke.
- **Cross-source identity resolution.** "Priya" in one transcript and
  "P. Sharma" in a chat export are not merged. A stretch item, deliberately not
  attempted, because the merge rule would need to be measured rather than
  assumed.
