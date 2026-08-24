# There is no committed measurement in this repository right now

`eval/results.txt` and `eval/results.json` are absent on purpose. This file
exists so their absence reads as a decision rather than an oversight.

## Why

The free tier for `gemini-3.6-flash` is **20 requests per day per project**. A
full evaluation run costs roughly eighteen. On 24 August 2026 two keys were
spent trying to produce one clean run, and every attempt ended the same way: the
extraction chunks completed, and the five retrieval questions were refused with
429 before the model ever saw them.

The harness refuses to write a results file when any chunk **or question** could
not be completed, so it wrote nothing. That is the guard working. A run whose
questions were rate-limited reports `answers carrying a verified citation 1/5`
for a capability that is not broken, and committing that number would be
reporting a quota artefact as a quality result.

## What was removed, and why it was wrong to have committed it

An earlier run on the same day *was* committed before the guard understood
questions. It reported:

    6d  Answers carrying a verified citation   1/5   FAIL

Three of those five questions were never asked. `answer_question` swallows the
rate-limit error and returns a not-found, which is indistinguishable in the
metric from the model looking and finding nothing. `Answer.model_failed` now
carries the difference and the guard reads it.

## To produce one

One full run, on a key whose daily quota is untouched:

    make eval

It writes `eval/results.txt` and `eval/results.json`, or it refuses and says
which file it left alone. Nothing else needs to be done, and nothing in the
README should quote a number until it does.

Extraction responses are cached, so most of a re-run costs nothing. The
questions are the part that needs live calls.
