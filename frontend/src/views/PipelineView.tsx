/**
 * The pipeline, shown rather than described.
 *
 * Every stage between a file and a model call is here with its real output:
 * the normalised source text that quote verification runs against, the
 * segments parsed out of it, the chunks exactly as the provider receives them,
 * and the vector index built over them.
 *
 * The chunk panel is the one that matters. The brief says chunking decides
 * extraction quality and that it will be asked about, and a claim about
 * overlap and context headers is worth less than the bytes themselves. What is
 * rendered here is byte-for-byte what went to Gemini.
 */

import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { Chunk, IndexStats, Segment, Source } from "../api/types";
import { Badge, Button, Empty, Field, Panel } from "../components/ui";

type Stage = "text" | "segments" | "chunks" | "index";

const STAGES: { id: Stage; label: string; caption: string }[] = [
  { id: "text", label: "1 · Source text", caption: "Whitespace-normalised. Every quote is checked against this exact string." },
  { id: "segments", label: "2 · Segments", caption: "One speaker turn each, with the character span a citation points at." },
  { id: "chunks", label: "3 · Chunks", caption: "Byte for byte what the model receives." },
  { id: "index", label: "4 · Vector index", caption: "all-MiniLM-L6-v2 over every consented segment and approved extraction." },
];

export function PipelineView() {
  const [sources, setSources] = useState<Source[]>([]);
  const [sourceId, setSourceId] = useState<string>("");
  const [stage, setStage] = useState<Stage>("chunks");

  const [text, setText] = useState<{ text: string; length: number } | null>(null);
  const [segments, setSegments] = useState<Segment[]>([]);
  const [chunks, setChunks] = useState<Chunk[]>([]);
  const [index, setIndex] = useState<IndexStats | null>(null);
  const [openChunk, setOpenChunk] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [rebuilding, setRebuilding] = useState(false);

  useEffect(() => {
    api.sources().then((all) => {
      const usable = all.filter((s) => s.status === "ingested");
      setSources(usable);
      if (usable.length && !sourceId) setSourceId(usable[0].id);
    });
    api.indexStats().then(setIndex).catch(() => setIndex(null));
  }, []);

  useEffect(() => {
    if (!sourceId) return;
    setLoading(true);
    Promise.all([api.sourceText(sourceId), api.segments(sourceId), api.chunks(sourceId)])
      .then(([t, s, c]) => {
        setText(t);
        setSegments(s);
        setChunks(c);
        setOpenChunk(c[0]?.id ?? null);
      })
      .finally(() => setLoading(false));
  }, [sourceId]);

  const source = sources.find((s) => s.id === sourceId) ?? null;

  return (
    <div className="space-y-4">
      <Panel
        title="Pipeline"
        subtitle="What each stage actually produced, not what it is supposed to produce"
        actions={
          <select
            className="rounded border border-[var(--color-line)] px-2 py-1 text-xs"
            value={sourceId}
            onChange={(e) => setSourceId(e.target.value)}
          >
            {sources.map((s) => (
              <option key={s.id} value={s.id}>
                {s.title}
              </option>
            ))}
          </select>
        }
      >
        <div className="flex flex-wrap gap-1 border-b border-[var(--color-line)] px-4 py-2">
          {STAGES.map((s) => (
            <Button key={s.id} tone={stage === s.id ? "info" : "neutral"} onClick={() => setStage(s.id)}>
              {s.label}
            </Button>
          ))}
        </div>
        <p className="px-4 py-2 text-xs text-[var(--color-muted)]">
          {STAGES.find((s) => s.id === stage)?.caption}
        </p>

        {loading && (
          <div className="flex items-center gap-2 px-4 py-8 text-sm text-[var(--color-muted)]">
            <span className="h-3 w-3 animate-spin rounded-full border-2 border-[var(--color-line)] border-t-[var(--color-info)]" />
            reading the store…
          </div>
        )}

        {!loading && stage === "text" && text && (
          <div className="px-4 py-3">
            <div className="mb-2 flex gap-4 text-xs text-[var(--color-muted)]">
              <span>{text.length.toLocaleString()} characters</span>
              <span>·</span>
              <span>speaker labels and timestamps are excluded on purpose</span>
            </div>
            <pre className="quote max-h-96 overflow-auto whitespace-pre-wrap rounded bg-[var(--color-canvas)] p-3">
              {text.text}
            </pre>
          </div>
        )}

        {!loading && stage === "segments" && (
          <div className="max-h-96 overflow-auto">
            <table className="w-full text-xs">
              <thead className="sticky top-0 bg-[var(--color-surface)]">
                <tr className="border-b border-[var(--color-line)] text-left uppercase tracking-wide text-[var(--color-muted)]">
                  <th className="px-4 py-2 font-medium">#</th>
                  <th className="px-2 py-2 font-medium">Time</th>
                  <th className="px-2 py-2 font-medium">Speaker</th>
                  <th className="px-2 py-2 font-medium">Chars</th>
                  <th className="px-4 py-2 font-medium">Text</th>
                </tr>
              </thead>
              <tbody>
                {segments.map((s) => (
                  <tr key={s.id} className="border-b border-[var(--color-line)] last:border-0 align-top">
                    <td className="px-4 py-1.5 tabular-nums text-[var(--color-muted)]">{s.segment_index}</td>
                    <td className="px-2 py-1.5 whitespace-nowrap tabular-nums">{s.start_ts ?? "—"}</td>
                    <td className={`px-2 py-1.5 whitespace-nowrap ${s.speaker ? "" : "text-[var(--color-warn)]"}`}>
                      {s.speaker ?? "unattributed"}
                    </td>
                    <td className="px-2 py-1.5 whitespace-nowrap tabular-nums text-[var(--color-muted)]">
                      {s.char_start}–{s.char_end}
                    </td>
                    <td className="px-4 py-1.5">{s.text}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {!loading && stage === "chunks" && (
          <div className="space-y-2 px-4 py-3">
            <div className="flex flex-wrap gap-3 text-xs text-[var(--color-muted)]">
              <span>{chunks.length} chunks</span>
              <span>·</span>
              <span>{segments.length} segments in</span>
              <span>·</span>
              <span>
                {chunks.reduce((n, c) => n + c.segment_ids.length, 0)} segment slots out, the difference
                being the deliberate overlap
              </span>
            </div>

            {chunks.map((chunk) => (
              <div key={chunk.id} className="rounded border border-[var(--color-line)]">
                <button
                  type="button"
                  onClick={() => setOpenChunk(openChunk === chunk.id ? null : chunk.id)}
                  className="flex w-full items-center justify-between gap-3 px-3 py-2 text-left hover:bg-[var(--color-canvas)]"
                >
                  <span className="text-sm font-medium">
                    Chunk {chunk.index + 1} of {chunk.total}
                    <span className="ml-2 font-normal text-xs text-[var(--color-muted)]">
                      segments {chunk.first_segment_index}–{chunk.last_segment_index} · {chunk.start_ts}–
                      {chunk.end_ts} · ~{chunk.estimated_tokens} tokens
                    </span>
                  </span>
                  <span className="flex shrink-0 gap-1">
                    {chunk.overlap_segment_ids.length > 0 && (
                      <Badge tone="info">{chunk.overlap_segment_ids.length} carried over</Badge>
                    )}
                    <Badge>{openChunk === chunk.id ? "hide" : "show"}</Badge>
                  </span>
                </button>

                {openChunk === chunk.id && (
                  <div className="border-t border-[var(--color-line)]">
                    <div className="border-b border-[var(--color-line)] bg-[var(--color-warn-bg)] px-3 py-2">
                      <div className="mb-1 text-[11px] uppercase tracking-wide text-[var(--color-warn)]">
                        Context header — background only, never quotable
                      </div>
                      <pre className="quote whitespace-pre-wrap text-[var(--color-ink)]">{chunk.context}</pre>
                    </div>
                    <div className="px-3 py-2">
                      <div className="mb-1 text-[11px] uppercase tracking-wide text-[var(--color-muted)]">
                        Transcript — the only text a quote may come from
                      </div>
                      <pre className="quote max-h-80 overflow-auto whitespace-pre-wrap">
                        {chunk.text.split("\n").map((line, i) => {
                          const carried = i < chunk.overlap_segment_ids.length;
                          return (
                            <div
                              key={i}
                              className={carried ? "bg-[var(--color-info-bg)]" : ""}
                              title={carried ? "carried over from the previous chunk" : undefined}
                            >
                              {line}
                            </div>
                          );
                        })}
                      </pre>
                    </div>
                  </div>
                )}
              </div>
            ))}
          </div>
        )}

        {!loading && stage === "index" && (
          <div className="px-4 py-3">
            {index && !index.index_path ? (
              <Empty>No index has been built yet.</Empty>
            ) : (
              index && (
                <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
                  <Field label="Vectors">{index.vectors.toLocaleString()}</Field>
                  <Field label="Dimensions">{index.dimensions}</Field>
                  <Field label="By type">
                    {Object.entries(index.by_type)
                      .map(([k, v]) => `${v} ${k}`)
                      .join(", ") || "—"}
                  </Field>
                  <Field label="Built">
                    {index.built_at ? new Date(index.built_at).toLocaleString() : "—"}
                  </Field>
                </div>
              )
            )}
            <p className="mt-3 text-xs text-[var(--color-muted)]">
              Model: {index?.model ?? "—"}. Exact search over unit vectors, so the inner product is a
              cosine similarity. Flat rather than approximate: at this corpus size an approximate index
              would trade recall for a speed nobody needs.
            </p>
            <div className="mt-3">
              <Button
                tone="info"
                disabled={rebuilding}
                onClick={() => {
                  setRebuilding(true);
                  api
                    .rebuildIndex()
                    .then(setIndex)
                    .finally(() => setRebuilding(false));
                }}
              >
                {rebuilding ? "re-encoding…" : "Rebuild index"}
              </Button>
            </div>
          </div>
        )}
      </Panel>

      {source && (
        <p className="text-xs text-[var(--color-muted)]">
          Everything above is computed from the store on request, using the settings a real run would
          use. Changing the chunk size in .env and reloading shows the new chunking immediately.
        </p>
      )}
    </div>
  );
}
