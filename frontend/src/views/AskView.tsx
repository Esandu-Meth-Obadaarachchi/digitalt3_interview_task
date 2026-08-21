/**
 * M8, and the retrieval underneath it.
 *
 * Two panels rather than one, because they answer different questions. The
 * retrieval panel shows what was found and by which method, with the keyword
 * rank, the dense rank and the fused score on every hit. The answer panel
 * shows what the model made of it, with each claim next to the quote that
 * backs it.
 *
 * Seeing that a result came from keyword alone, or from dense alone, is what
 * makes the case for hybrid legible rather than asserted. It is also how you
 * tell a retrieval failure from a reasoning failure when an answer is wrong.
 */

import { useState } from "react";
import { api, ApiFailure } from "../api/client";
import type { Answer, SearchHit } from "../api/types";
import { Badge, Button, Empty, ErrorNote, Panel } from "../components/ui";

const MODES = ["hybrid", "keyword", "dense"] as const;

const SUGGESTIONS = [
  "When did we agree to defer the reporting module?",
  "Why did we switch from WebSockets to SSE for the dashboard?",
  "Who is responsible for the load testing of the payments service?",
  "What database did we decide to use for the user analytics module?",
];

const FOUND_BY_TONE = { both: "ok", keyword: "info", dense: "warn" } as const;

function foundBy(hit: SearchHit): "both" | "keyword" | "dense" {
  if (hit.keyword_rank !== null && hit.dense_rank !== null) return "both";
  return hit.keyword_rank !== null ? "keyword" : "dense";
}

export function AskView() {
  const [question, setQuestion] = useState("");
  const [mode, setMode] = useState<(typeof MODES)[number]>("hybrid");
  const [hits, setHits] = useState<SearchHit[] | null>(null);
  const [answer, setAnswer] = useState<Answer | null>(null);
  const [stage, setStage] = useState("");
  const [error, setError] = useState<{ code?: string; message: string } | null>(null);

  const run = async (withModel: boolean) => {
    if (!question.trim()) return;
    setError(null);
    setAnswer(null);
    setHits(null);
    try {
      setStage("retrieving");
      const found = await api.retrieve(question, mode, 8);
      setHits(found);
      if (!withModel) return;

      setStage("asking the model, then verifying every quote against the source it cites");
      setAnswer(await api.ask(question, mode, 8));
    } catch (exc) {
      setError(exc instanceof ApiFailure ? { code: exc.code, message: exc.message } : { message: String(exc) });
    } finally {
      setStage("");
    }
  };

  return (
    <div className="space-y-4">
      <Panel title="Ask" subtitle="Answers come only from stored transcripts and approved extractions">
        <div className="space-y-3 px-4 py-3">
          <div className="flex flex-wrap gap-2">
            <input
              className="min-w-64 flex-1 rounded border border-[var(--color-line)] px-2 py-1.5 text-sm"
              placeholder="When did we agree to defer the reporting module?"
              value={question}
              onChange={(e) => setQuestion(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && run(true)}
            />
            <div className="flex gap-1">
              {MODES.map((m) => (
                <Button key={m} tone={mode === m ? "info" : "neutral"} onClick={() => setMode(m)}>
                  {m}
                </Button>
              ))}
            </div>
            <Button tone="ok" disabled={!question.trim() || Boolean(stage)} onClick={() => run(true)}>
              Ask
            </Button>
            <Button
              disabled={!question.trim() || Boolean(stage)}
              onClick={() => run(false)}
              title="Retrieval only. No model call, so it costs nothing against the daily quota."
            >
              Retrieve only
            </Button>
          </div>

          <div className="flex flex-wrap gap-1">
            {SUGGESTIONS.map((s) => (
              <button
                key={s}
                type="button"
                onClick={() => setQuestion(s)}
                className="rounded border border-[var(--color-line)] px-2 py-0.5 text-xs text-[var(--color-muted)] hover:bg-[var(--color-canvas)]"
              >
                {s.length > 52 ? `${s.slice(0, 52)}…` : s}
              </button>
            ))}
          </div>

          {stage && (
            <div className="flex items-center gap-2 text-xs text-[var(--color-muted)]">
              <span className="h-3 w-3 animate-spin rounded-full border-2 border-[var(--color-line)] border-t-[var(--color-info)]" />
              {stage}…
            </div>
          )}

          <ErrorNote error={error} />
        </div>
      </Panel>

      {answer && (
        <Panel
          title={answer.found ? "Answer" : "Not found"}
          subtitle={
            answer.found
              ? `${answer.claims.length} claim(s), each verified against the source it cites`
              : "The correct response when nothing in the corpus supports an answer"
          }
          actions={
            <Badge tone={answer.found ? "ok" : "warn"}>{answer.found ? "cited" : "not found"}</Badge>
          }
        >
          <div className="px-4 py-3">
            <p className={`text-sm ${answer.found ? "" : "text-[var(--color-warn)]"}`}>{answer.answer}</p>
          </div>

          {answer.claims.length > 0 && (
            <ul className="divide-y divide-[var(--color-line)] border-t border-[var(--color-line)]">
              {answer.claims.map((claim, index) => (
                <li key={index} className="px-4 py-3">
                  <p className="text-sm">{claim.statement}</p>
                  <blockquote className="quote mt-1.5 border-l-2 border-[var(--color-ok)] pl-3 text-[var(--color-muted)]">
                    “{claim.citation.quote}”
                  </blockquote>
                  <div className="mt-1 text-xs text-[var(--color-muted)]">
                    {claim.citation.source_title ?? claim.citation.source_id} ·{" "}
                    {claim.citation.speaker ?? "unattributed"} at {claim.citation.timestamp ?? "no timestamp"}
                    {claim.citation.char_start !== null && (
                      <> · characters {claim.citation.char_start}–{claim.citation.char_end}</>
                    )}
                  </div>
                </li>
              ))}
            </ul>
          )}

          {answer.dropped_claims.length > 0 && (
            <div className="border-t border-[var(--color-line)] px-4 py-2">
              <div className="text-[11px] uppercase tracking-wide text-[var(--color-bad)]">
                Dropped, because the quote did not verify
              </div>
              <ul className="mt-1 space-y-0.5 text-xs text-[var(--color-muted)]">
                {answer.dropped_claims.map((d, i) => (
                  <li key={i}>{d}</li>
                ))}
              </ul>
            </div>
          )}

          <div className="border-t border-[var(--color-line)] px-4 py-2 text-xs text-[var(--color-muted)]">
            {answer.retrieval_mode} retrieval · {answer.provider}:{answer.model} · prompt v
            {answer.prompt_version} · {answer.duration_ms} ms
          </div>
        </Panel>
      )}

      {hits && (
        <Panel
          title="What retrieval found"
          subtitle="Ranks from each method, and the fused score. No model involved."
        >
          {hits.length === 0 ? (
            <Empty>Nothing matched. With no sources there is nothing to answer from.</Empty>
          ) : (
            <ul className="divide-y divide-[var(--color-line)]">
              {hits.map((hit) => (
                <li key={`${hit.ref_type}:${hit.ref_id}`} className="px-4 py-2.5">
                  <div className="flex flex-wrap items-center gap-2 text-xs">
                    <Badge tone={FOUND_BY_TONE[foundBy(hit)]}>{foundBy(hit)}</Badge>
                    {hit.keyword_rank !== null && (
                      <span className="text-[var(--color-muted)]">keyword #{hit.keyword_rank}</span>
                    )}
                    {hit.dense_rank !== null && (
                      <span className="text-[var(--color-muted)]">
                        dense #{hit.dense_rank}
                        {hit.dense_score !== null && ` (${hit.dense_score.toFixed(3)})`}
                      </span>
                    )}
                    <span className="text-[var(--color-muted)]">score {hit.score.toFixed(4)}</span>
                    {hit.ref_type === "extraction" && <Badge tone="ok">approved extraction</Badge>}
                  </div>
                  <p className="mt-1 text-sm">{hit.text}</p>
                  <div className="mt-0.5 text-xs text-[var(--color-muted)]">
                    {hit.source_title ?? hit.source_id} · {hit.speaker ?? "unattributed"} at{" "}
                    {hit.timestamp ?? "no timestamp"}
                  </div>
                </li>
              ))}
            </ul>
          )}
        </Panel>
      )}
    </div>
  );
}
