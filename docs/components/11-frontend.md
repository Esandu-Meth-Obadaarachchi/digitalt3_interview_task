# 11 · The review interface

**Capability:** none directly — it is the surface M6 is reviewed through
**Code:** `frontend/` — Vite 8, React 19, TypeScript 6, Tailwind 4
**Tests:** none. A deliberate cut, recorded below.

> *"The review-and-approve surface matters far more than how pretty it looks.
> Zero marks for visual polish; real marks for a usable approval queue."*

---

## Seven tabs

| Tab | Shows |
|---|---|
| **Sources** | Upload, ingestion outcomes, defects, consent evidence |
| **Pipeline** | Source text → segments → **chunks byte-for-byte** → vector index |
| **Channels** | M9 classification, and the count of excluded direct messages |
| **Review queue** | M6 — the surface that matters |
| **Tracker** | M7 items, write attempts, the raw JSONL log |
| **Digests & records** | M10 schedule and digests, M11 records |
| **Ask** | M8 retrieval and answers with citations |

---

## Decisions worth defending

### Every type renders on its own terms

An action, a decision, a risk and a signal are **not the same object with a
different label.** They carry different fields and a reviewer checks different
things, so the queue asks each type for its headline and the facts that belong
beside it — rather than reaching for `payload.what` and rendering blanks for two
thirds of the queue.

| Type | Headline | Facts |
|---|---|---|
| action | `what` | owner, due |
| decision | `what_was_decided` | who stated it, rationale |
| risk | `description` | severity, area, owner |
| signal | `text` | class, channel, author |

### `UNSPECIFIED` is amber, never an empty cell

With a tooltip saying the source did not state it. **Abstention is an answer,
not missing data**, and rendering it as a blank would undo the discipline every
other layer maintains.

### Quotes are monospaced

**A verbatim quote is evidence and should not look like prose the interface
wrote.**

### Backend errors are surfaced verbatim

A refused approval and a refused consent are **the system working**. The reason
has to reach the reviewer rather than becoming "something went wrong".

### The consent control on upload has no default

The form cannot be submitted until granted or withheld is chosen. Consent is a
property of the source, and **pre-ticking "granted" would quietly make the whole
guarantee meaningless.**

Uploading *without* consent is deliberately allowed, and the helper text says
what will happen: the file is written, refused before it is opened, deleted
again, and the report comes back with zero bytes read. **Worth doing on camera
rather than describing.**

### The pipeline tab shows real output, not a description

The brief says chunking decides extraction quality and will be asked about. **The
bytes are worth more than a description of them.** The context header appears in
its own block marked non-quotable; carried-over overlap lines are highlighted.

Computed from the store on request using the settings a real run would use — so
changing `CHUNK_MAX_TOKENS` and reloading shows the new chunking immediately.

### The Ask tab separates retrieval from answering

Two panels, because they answer different questions. Retrieval shows every hit
with its **keyword rank, dense rank, cosine and fused score**, and whether one
method or both found it.

That is what makes the case for hybrid **legible rather than asserted** — and it
is how you tell a retrieval failure from a reasoning failure when an answer is
wrong.

**"Retrieve only"** runs search with no model call, so it costs nothing against
the daily quota.

### The scheduler is shown before the button that runs the job early

Deliberate. The brief calls a button with nothing behind it a partial
implementation, so the **next fire times come first.**

### Digest sections are shown in the order they are *filled*

Attention, to decide, then moved — not the order they are named. That is the
precedence that decides which section an item lands in when it qualifies for
two, and showing them in that order **makes the rule visible rather than
surprising.**

---

## Architecture

```
frontend/src/
├── api/
│   ├── types.ts      hand-written mirrors of the Pydantic contracts
│   └── client.ts     the one place the frontend calls the backend
├── components/
│   ├── ui.tsx                 badges, panels, buttons, the UNSPECIFIED renderer
│   ├── extractionKinds.tsx    the per-type view model
│   └── UploadPanel.tsx
└── views/            one per tab
```

**Types are hand-written rather than generated.** The surface is small, and a
hand-written type read alongside the Python contract is easier to keep honest
than a generation step nobody re-runs.

**One colour system, applied consistently.** State carries colour: verified and
approved green, unverified and rejected red, pending and abstention amber. A
reviewer scanning the queue should be able to read state without reading words.

**Theme:** a single light palette defined once as CSS custom properties. No dark
mode — the brief awards no marks for visual polish and it would double the
surface to keep consistent.

---

## What it does not do

- **No automated tests.** A deliberate cut. The brief awards no marks for the
  interface, and the review surface is exercised through the API tests
  underneath it. Recorded in `decision_log.md` L22.
- **No authentication.** The reviewer is a name in a text field.
- **No optimistic updates.** Every action round-trips and reloads. Slower, and
  it means what is on screen is what the server holds — which matters more here
  than responsiveness.
- **No bulk actions.** Approving twenty items in one click is approving nothing.
