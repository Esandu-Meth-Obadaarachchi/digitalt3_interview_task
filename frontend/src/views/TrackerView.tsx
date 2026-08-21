/**
 * M7 made inspectable.
 *
 * Three things a reviewer needs to be able to see, and one they need to be
 * able to prove:
 *
 *   what the tracker holds, with the agent's items distinguishable from the
 *   pre-existing backlog it did not create
 *   every write attempt, including the deduplicated and the blocked ones
 *   the raw JSONL log, which is the artefact to open during a walkthrough
 *
 * The thing to prove is that nothing reached the tracker without approval.
 * "Blocked" attempts are shown in red beside the successful ones, which is
 * what makes the gate visible rather than merely asserted.
 */

import { useEffect, useState } from "react";
import { api, ApiFailure } from "../api/client";
import type { TrackerItem, TrackerSummary, WriteAttempt } from "../api/types";
import { Badge, Button, Empty, ErrorNote, Field, Panel } from "../components/ui";

const OUTCOME_TONE = { created: "ok", deduplicated: "info", blocked: "bad" } as const;

export function TrackerView() {
  const [summary, setSummary] = useState<TrackerSummary | null>(null);
  const [items, setItems] = useState<TrackerItem[]>([]);
  const [attempts, setAttempts] = useState<WriteAttempt[]>([]);
  const [log, setLog] = useState<Record<string, unknown>[]>([]);
  const [origin, setOrigin] = useState<"agent" | "seeded" | "all">("agent");
  const [selected, setSelected] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<{ code?: string; message: string } | null>(null);

  const load = () => {
    const writtenByAgent = origin === "all" ? undefined : origin === "agent";
    api.trackerSummary().then(setSummary).catch(() => setSummary(null));
    api.trackerItems({ written_by_agent: writtenByAgent }).then(setItems).catch(() => setItems([]));
    api.trackerAttempts().then(setAttempts).catch(() => setAttempts([]));
    api.trackerWriteLog(60).then(setLog).catch(() => setLog([]));
  };

  useEffect(load, [origin]);

  const sync = async () => {
    setBusy(true);
    setError(null);
    try {
      await api.trackerSync();
      load();
    } catch (exc) {
      setError(exc instanceof ApiFailure ? { code: exc.code, message: exc.message } : { message: String(exc) });
    } finally {
      setBusy(false);
    }
  };

  const active = items.find((item) => item.external_ref === selected) ?? null;

  return (
    <div className="space-y-4">
      {summary && (
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-5">
          {(
            [
              ["Written by the agent", summary.items_written_by_agent, "ok"],
              ["Pre-existing backlog", summary.items_pre_existing, "neutral"],
              ["Audited writes", summary.audited_writes, "info"],
              ["Deduplicated", summary.attempts.deduplicated ?? 0, "info"],
              ["Blocked by the gate", summary.attempts.blocked ?? 0, "bad"],
            ] as const
          ).map(([label, value, tone]) => (
            <div key={label} className="rounded-lg border border-[var(--color-line)] bg-[var(--color-surface)] px-3 py-2">
              <div className="text-[11px] uppercase tracking-wide text-[var(--color-muted)]">{label}</div>
              <div className="mt-0.5 flex items-baseline gap-2">
                <span className="text-xl font-semibold tabular-nums">{value}</span>
                {label === "Blocked by the gate" && value > 0 && <Badge tone={tone}>refused</Badge>}
              </div>
            </div>
          ))}
        </div>
      )}

      <ErrorNote error={error} />

      <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
        <Panel
          title="Tracker contents"
          subtitle="The backlog is data the agent never created, and must work alongside"
          actions={
            <div className="flex gap-1">
              {(
                [
                  ["agent", "Agent"],
                  ["seeded", "Backlog"],
                  ["all", "All"],
                ] as const
              ).map(([value, label]) => (
                <Button key={value} tone={origin === value ? "info" : "neutral"} onClick={() => setOrigin(value)}>
                  {label}
                </Button>
              ))}
              <Button tone="ok" disabled={busy} onClick={sync} title="Re-runnable. Running it twice must not create duplicates.">
                {busy ? "syncing…" : "Sync approved"}
              </Button>
            </div>
          }
        >
          {items.length === 0 ? (
            <Empty>Nothing here yet. Approve an action to write one.</Empty>
          ) : (
            <ul className="max-h-[26rem] divide-y divide-[var(--color-line)] overflow-y-auto">
              {items.map((item) => (
                <li key={item.external_ref}>
                  <button
                    type="button"
                    onClick={() => setSelected(item.external_ref)}
                    className={`w-full px-4 py-2.5 text-left transition-colors hover:bg-[var(--color-canvas)] ${
                      selected === item.external_ref ? "bg-[var(--color-canvas)]" : ""
                    }`}
                  >
                    <div className="flex items-start justify-between gap-3">
                      <span className="text-sm">
                        <span className="font-mono text-xs text-[var(--color-muted)]">{item.external_ref}</span>{" "}
                        {item.title}
                      </span>
                      {item.source_ref ? <Badge tone="ok">agent</Badge> : <Badge>pre-existing</Badge>}
                    </div>
                    <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-[var(--color-muted)]">
                      {/* status is rendered exactly as stored, whitespace and all */}
                      <span className="rounded bg-[var(--color-canvas)] px-1 font-mono">“{item.status}”</span>
                      <span>·</span>
                      <span className={item.assignee ? "" : "text-[var(--color-warn)]"}>
                        {item.assignee ?? "unassigned"}
                      </span>
                      <span>·</span>
                      <span className={item.due_date ? "" : "text-[var(--color-warn)]"}>
                        {item.due_date ?? "no due date"}
                      </span>
                    </div>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </Panel>

        <div className="space-y-4">
          {active && (
            <Panel title={active.external_ref} subtitle={active.title}>
              <div className="grid grid-cols-2 gap-4 px-4 py-3">
                <Field label="Status">“{active.status}”</Field>
                <Field label="Assignee">{active.assignee ?? "unassigned"}</Field>
                <Field label="Due">{active.due_date ?? "none"}</Field>
                <Field label="Origin">{active.source_ref ? "written by the agent" : "pre-existing"}</Field>
              </div>
              {active.description && (
                <div className="border-t border-[var(--color-line)] px-4 py-3">
                  <pre className="quote whitespace-pre-wrap text-[var(--color-muted)]">{active.description}</pre>
                </div>
              )}
              {active.labels.length > 0 && (
                <div className="flex flex-wrap gap-1 border-t border-[var(--color-line)] px-4 py-2">
                  {active.labels.map((label) => (
                    <Badge key={label} tone={label.startsWith("needs-") || label === "unverified-quote" ? "warn" : "neutral"}>
                      {label}
                    </Badge>
                  ))}
                </div>
              )}
            </Panel>
          )}

          <Panel title="Write attempts" subtitle="Created, deduplicated and blocked alike">
            {attempts.length === 0 ? (
              <Empty>No write has been attempted yet.</Empty>
            ) : (
              <ul className="max-h-64 divide-y divide-[var(--color-line)] overflow-y-auto">
                {attempts.map((attempt) => (
                  <li key={attempt.id} className="px-4 py-2 text-xs">
                    <div className="flex items-center gap-2">
                      <Badge tone={OUTCOME_TONE[attempt.outcome]}>{attempt.outcome}</Badge>
                      <span className="font-mono">{attempt.external_ref ?? "—"}</span>
                      <span className="truncate text-[var(--color-muted)]">
                        {attempt.extraction_id.split("::").pop()}
                      </span>
                    </div>
                    {attempt.reason && <p className="mt-1 text-[var(--color-muted)]">{attempt.reason}</p>}
                  </li>
                ))}
              </ul>
            )}
          </Panel>

          <Panel title="write_log/tracker_writes.jsonl" subtitle="The raw artefact, appended one line per attempt">
            {log.length === 0 ? (
              <Empty>The log is empty.</Empty>
            ) : (
              <pre className="quote max-h-64 overflow-auto px-4 py-3 text-[var(--color-muted)]">
                {log.map((line) => JSON.stringify(line)).join("\n")}
              </pre>
            )}
          </Panel>
        </div>
      </div>
    </div>
  );
}
