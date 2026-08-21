# 07 · Retrieval and question answering

**Capability:** M8
**Code:** `retrieval/embeddings.py`, `vector_index.py`, `search.py`, `qa.py`
**Tests:** 5 in `test_quote_and_dates.py` (neighbour expansion), plus harness cases 6, 6b, 6c, 6d

---

## The flow

```
  question
     ├──► SQLite FTS5, BM25, porter stemming ──► ranked list A
     └──► FAISS IndexFlatIP over MiniLM 384d ──► ranked list B
                        │
              Reciprocal Rank Fusion (by RANK, not score)
                        │
              + the turns either side, as separate sources
                        │
              LLM answers in claims, each carrying a quote
                        │
              verify each quote against THE SOURCE IT CITES
                        │
     ┌──────────────────┴──────────────────┐
     ▼                                      ▼
  answer with citations              NOT FOUND
```

---

## Measured

**5/5 correct source in the top three. 5/5 answers carry a verified citation.
The unanswerable question correctly refused** — even though the corpus *does*
discuss databases, just not for a user analytics module. That is exactly the
trap the prompt was written against.

### All three modes, on every run, at no model cost

| Mode | Correct source | Correct **segment** | Mean rank of the cited segment |
|---|---|---|---|
| keyword (FTS5 / BM25) | 5/5 | 4/5 | 2.4 |
| dense (FAISS / MiniLM) | 5/5 | **5/5** | **2.0** |
| hybrid (RRF) | 5/5 | 4/5 | **2.0** |

**Two findings reported rather than dressed up.**

**The brief's metric saturates.** All three put the correct source in the top
three — five questions over two transcripts does not discriminate at source
granularity. A stricter segment-level metric was added, since a citation points
at a segment rather than at a meeting.

**Hybrid does not beat dense here.** Dense is ahead by one question and level on
mean rank. Hybrid is kept as the default on an argument that is **explicitly not
measured**: the two fail in different directions, and keyword catches exact
tokens (names, dates, identifiers) where dense is weakest. **None of the five
golden questions probes that.**

Adding questions designed to favour hybrid *after* seeing it lose would be
fishing, so they were not added. **On today's evidence dense would win**, and the
README says so.

---

## Why fusion is by rank, not score

BM25 scores and cosine similarities are **not comparable quantities**. Any
weighted sum needs a normalisation constant invented out of nothing — and that
constant would then be tuned on the golden set and quietly reported as a result.

```
score(d) = Σ over lists of 1 / (k + rank(d))
```

RRF has no such parameter. `k` is left at the conventional 60 for the same
reason.

Each half is asked for **three times** the final list length, so a result ranked
mid-table by both still has the chance to win on fusion rather than being cut
before the fusion sees it.

---

## The bug found by using the interface

Asking *"what happened with the hotel app project, what were the tasks in it"*
returned **not found**.

**The model was right to refuse.** None of the eight retrieved sources contained
the answer, and it declined rather than assembling something plausible.

The defect was in retrieval:

| | Rank |
|---|---|
| The three individual assignment turns | 16, 31, 46 |
| The closing recap, restating all of them | **#9** |
| Sources sent to the model | **8** |

**The answer missed the cut by one place.**

### Why neither method could reach them

The turn that *matches* a question is often the one **asking** it, and the answer
is what somebody said next.

*"How do we split the work for Phase 1?"* retrieves at rank 1. The assignments
are the three turns after it — *"I'll take the booking engine"* — which share
**no words** with the question and **no embedding neighbourhood** with it.

### The fix: neighbour expansion

Each retrieved segment now brings the turns either side of it.

```
neighbours=0    8 sources, recap absent,  not-found (correctly)
neighbours=1   19 sources, recap at #17,  answered with 5 verified citations
```

On the case it was designed for:

```
"how are we splitting the work for phase 1"
neighbours=0    0/3 assignment turns present
neighbours=1    2/3 assignment turns present
```

**What it does not do**, recorded rather than glossed: it still does not surface
the three individual assignment turns for the vague question. It surfaces the
recap, which happens to be enough. A vague multi-part question remains a
retrieval problem expansion only partly solves.

### A hypothesis checked and dropped

I expected the answer was locked behind the **approval gate** — the extracted
actions *are* "the tasks", and only approved extractions are indexed.

Approved all five, rebuilt the index (148 → 153 vectors), re-ran: **none reached
the top sources.** Expansion alone did it. Recorded because the wrong
explanation was plausible and would have been easy to assert.

### Two design points

**Neighbours are separate numbered sources**, never a widening of the matched
hit's text. Widening would let the model quote a neighbour while citing the
matched segment — a citation that verifies against the corpus and points at the
wrong line. **Worse than no citation, because it looks checkable.**

**The mode comparison runs with expansion off.** It would flatter every mode
equally and hide which one actually found the cited segment, which is the only
thing that comparison exists to show.

---

## Grounding

**Every quote is verified against the source it cites**, not against the corpus.
A quote appearing in source 4 while the claim cites source 2 would pass a
corpus-wide check.

**Verification runs twice.** Once as a validator inside the retry loop, so a bad
quote is repaired. Once afterwards, so the stored answer is verified by code that
did not also produce it.

**An answer whose claims all fail verification becomes a not-found**, not a
fluent paragraph with the citations quietly removed. The system would rather say
nothing than say something a reader cannot check, **because a reader cannot tell
a real answer from an invented one and will believe both.**

`found: false` is a **successful response**, not an error.

---

## Index design

`IndexFlatIP` over unit-length vectors, making the inner product a cosine
similarity. **Flat means exact search, no training, no approximation.**

That is a choice, not a default. The corpus is 153 vectors. An approximate index
exists to trade recall for speed at a scale this is nowhere near, and using one
would mean reporting accuracy that is **partly a property of the index rather
than of the embeddings.**

The mapping back to rows lives in `embedding_index` with the model that produced
each vector and a hash of the text. The model name is part of the index
filename, so vectors from one model can never be loaded under another. **If the
index and its mapping disagree on length, loading is refused outright** —
returning citations that point at the wrong rows is the one failure this system
must not have.

**Only approved extractions are indexed** alongside transcript segments.
Answering from an unapproved one would route around the approval gate.

---

## Two embedders, one interface

| | Used for | Notion of meaning |
|---|---|---|
| `MiniLMEmbedder` | every reported number | yes |
| `HashingEmbedder` | the test suite | **no** |

Measured:

```
MiniLM   sim("defer the reporting module", "postpone the reporting work") = 0.583
hashing  the same pair                                                    = 0.000
```

`HashingEmbedder` is **not a mock** — it produces genuine unit vectors with real
cosine geometry, so FAISS, the search and the fusion are exercised for real. It
exists because loading a transformer would put two seconds on every test that
touches retrieval.

**Retrieval quality is never asserted in the suite**, only mechanics. The harness
records which embedder produced a comparison so the distinction cannot be missed.

---

## What it does not do

- **No reranker.** A cross-encoder pass would be marginally better ranking,
  meaningfully slower on 8 GB, and hard to defend as necessary at this corpus
  size.
- **No query decomposition.** A multi-part question is sent as one query. The
  hotel-app question showed why that matters; recorded as a limitation.
- **No conversational memory.** Each question is independent.
