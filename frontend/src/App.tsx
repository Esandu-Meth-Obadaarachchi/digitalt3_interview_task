import { useEffect, useState } from "react";
import { api } from "./api/client";
import type { Health } from "./api/types";
import { Badge } from "./components/ui";
import { ReviewView } from "./views/ReviewView";
import { SourcesView } from "./views/SourcesView";
import { AskView } from "./views/AskView";
import { ChannelsView } from "./views/ChannelsView";
import { PipelineView } from "./views/PipelineView";
import { TrackerView } from "./views/TrackerView";

type Tab = "sources" | "pipeline" | "channels" | "review" | "tracker" | "ask";

export default function App() {
  const [tab, setTab] = useState<Tab>("sources");
  const [health, setHealth] = useState<Health | null>(null);
  const [reviewer, setReviewer] = useState("esandu");
  const [refreshKey, setRefreshKey] = useState(0);

  useEffect(() => {
    api.health().then(setHealth).catch(() => setHealth(null));
  }, []);

  return (
    <div className="mx-auto flex min-h-full max-w-6xl flex-col gap-4 p-4">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-base font-semibold">Meeting &amp; Channel Intelligence Agent</h1>
          <p className="text-xs text-[var(--color-muted)]">
            Nothing is written anywhere until a person approves it
          </p>
        </div>

        <div className="flex items-center gap-3 text-xs">
          <label className="flex items-center gap-1.5">
            <span className="text-[var(--color-muted)]">reviewing as</span>
            <input
              className="w-24 rounded border border-[var(--color-line)] px-2 py-1"
              value={reviewer}
              onChange={(event) => setReviewer(event.target.value)}
            />
          </label>
          {health && (
            <div className="flex items-center gap-1.5">
              <Badge tone={health.llm_available ? "ok" : "bad"}>
                {health.llm_provider}:{health.llm_model}
              </Badge>
              {!health.llm_available && (
                <span className="text-[var(--color-bad)]" title={health.llm_detail}>
                  unavailable
                </span>
              )}
            </div>
          )}
        </div>
      </header>

      <nav className="flex gap-1 border-b border-[var(--color-line)]">
        {(
          [
            ["sources", "Sources"],
            ["pipeline", "Pipeline"],
            ["channels", "Channels"],
            ["review", "Review queue"],
            ["tracker", "Tracker"],
            ["ask", "Ask"],
          ] as const
        ).map(([value, label]) => (
          <button
            key={value}
            type="button"
            onClick={() => setTab(value)}
            className={`-mb-px border-b-2 px-3 py-2 text-sm transition-colors ${
              tab === value
                ? "border-[var(--color-ink)] font-medium"
                : "border-transparent text-[var(--color-muted)] hover:text-[var(--color-ink)]"
            }`}
          >
            {label}
          </button>
        ))}
      </nav>

      <main className="flex-1">
        {tab === "sources" && <SourcesView onExtracted={() => setRefreshKey((n) => n + 1)} />}
        {tab === "review" && <ReviewView key={refreshKey} reviewer={reviewer} />}
        {tab === "pipeline" && <PipelineView />}
        {tab === "channels" && <ChannelsView />}
        {tab === "tracker" && <TrackerView />}
        {tab === "ask" && <AskView />}
      </main>

      <footer className="border-t border-[var(--color-line)] pt-3 text-xs text-[var(--color-muted)]">
        {health ? (
          <>
            schema v{health.schema_version} · retrieval {health.retrieval_mode} · tracker{" "}
            {health.tracker_provider}
          </>
        ) : (
          <span className="text-[var(--color-bad)]">API unreachable. Start it with `make run`.</span>
        )}
      </footer>
    </div>
  );
}
