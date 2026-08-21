/**
 * M9 made visible.
 *
 * Two things a reviewer needs to see, and one they need to be able to prove.
 *
 * See: what survived classification, and what class each message got. Read:
 * the messages themselves, filtered by class.
 *
 * Prove: that no direct message reached the store. The messages themselves
 * leave no trace, so the count of what was excluded at ingestion is the only
 * evidence they were ever seen, and it is shown beside the channel list rather
 * than buried in a report.
 */

import { useEffect, useState } from "react";
import { api, ApiFailure } from "../api/client";
import type { ChatSummary, SignalClass, SignalRun, Source, StoredMessage } from "../api/types";
import { Badge, Button, Empty, ErrorNote, Field, Panel } from "../components/ui";

const CLASS_TONE = {
  blocker: "bad",
  request: "warn",
  decision: "ok",
  question: "info",
} as const;

const FILTERS: { label: string; value: SignalClass | "" }[] = [
  { label: "All", value: "" },
  { label: "Decisions", value: "decision" },
  { label: "Blockers", value: "blocker" },
  { label: "Requests", value: "request" },
  { label: "Questions", value: "question" },
];

export function ChannelsView() {
  const [exports_, setExports] = useState<Source[]>([]);
  const [sourceId, setSourceId] = useState("");
  const [summary, setSummary] = useState<ChatSummary | null>(null);
  const [messages, setMessages] = useState<StoredMessage[]>([]);
  const [excluded, setExcluded] = useState<number | null>(null);
  const [filter, setFilter] = useState<SignalClass | "">("");
  const [run, setRun] = useState<SignalRun | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<{ code?: string; message: string } | null>(null);

  useEffect(() => {
    api.sources().then((all) => {
      const chats = all.filter((s) => s.source_type === "chat_export");
      setExports(chats);
      if (chats.length && !sourceId) setSourceId(chats[0].id);
    });
  }, []);

  const load = () => {
    if (!sourceId) return;
    api.chatSummary(sourceId).then(setSummary).catch(() => setSummary(null));
    api
      .chatMessages({ source_id: sourceId, classification: filter || undefined })
      .then(setMessages)
      .catch(() => setMessages([]));
    api
      .report(sourceId)
      .then((r) => setExcluded(r.direct_messages_excluded))
      .catch(() => setExcluded(null));
  };

  useEffect(load, [sourceId, filter]);

  const classify = async () => {
    setBusy(true);
    setError(null);
    setRun(null);
    try {
      setRun(await api.classifySignals(sourceId));
      load();
    } catch (exc) {
      setError(exc instanceof ApiFailure ? { code: exc.code, message: exc.message } : { message: String(exc) });
    } finally {
      setBusy(false);
    }
  };

  const unclassified = summary?.by_class.unclassified ?? 0;

  return (
    <div className="space-y-4">
      <Panel
        title="Channels"
        subtitle="Direct messages are excluded by construction, not by a filter"
        actions={
          <div className="flex items-center gap-2">
            <select
              className="rounded border border-[var(--color-line)] px-2 py-1 text-xs"
              value={sourceId}
              onChange={(e) => setSourceId(e.target.value)}
            >
              {exports_.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.title}
                </option>
              ))}
            </select>
            <Button tone="info" disabled={busy || !sourceId} onClick={classify}>
              {busy ? "classifying…" : "Classify signals"}
            </Button>
          </div>
        }
      >
        <div className="grid grid-cols-2 gap-4 px-4 py-3 sm:grid-cols-4">
          <Field label="Channels">{summary?.channels.length ?? "—"}</Field>
          <Field label="Stored messages">{messages.length}</Field>
          <Field label="Awaiting classification">
            <span className={unclassified ? "text-[var(--color-warn)]" : ""}>{unclassified}</span>
          </Field>
          <Field label="Direct messages excluded">
            <span className="font-semibold text-[var(--color-ok)]">{excluded ?? "—"}</span>
          </Field>
        </div>

        <p className="border-t border-[var(--color-line)] px-4 py-2 text-xs text-[var(--color-muted)]">
          Those {excluded ?? 0} direct messages were dropped at parse time and were never written. The
          count is the only trace they existed, which is what makes "zero direct-message records" a
          claim you can check rather than take on trust. Noise is likewise discarded rather than
          stored, so anything below survived classification.
        </p>

        {busy && (
          <div className="flex items-center gap-2 border-t border-[var(--color-line)] px-4 py-3 text-xs text-[var(--color-muted)]">
            <span className="h-3 w-3 animate-spin rounded-full border-2 border-[var(--color-line)] border-t-[var(--color-info)]" />
            classifying in batches of twenty, per channel, in the order the messages were sent…
          </div>
        )}
      </Panel>

      <ErrorNote error={error} />

      {run && (
        <Panel title="Classification run" subtitle={`${run.provider}:${run.model} · prompt v${run.prompt_version}`}>
          <div className="grid grid-cols-2 gap-4 px-4 py-3 sm:grid-cols-5">
            <Field label="Batches">{run.batches}</Field>
            <Field label="Messages seen">{run.messages_seen}</Field>
            <Field label="Kept">{run.classified}</Field>
            <Field label="Noise discarded">{run.noise_discarded}</Field>
            <Field label="Queued for review">
              <span className="font-semibold">{run.queued}</span>
            </Field>
          </div>
          <div className="flex flex-wrap gap-1 border-t border-[var(--color-line)] px-4 py-2">
            {Object.entries(run.by_class).map(([label, count]) => (
              <Badge key={label} tone={CLASS_TONE[label as keyof typeof CLASS_TONE] ?? "neutral"}>
                {label} {count}
              </Badge>
            ))}
          </div>
          <p className="border-t border-[var(--color-line)] px-4 py-2 text-xs text-[var(--color-muted)]">
            Decisions, blockers and requests were queued because each could produce a downstream
            write. Questions were classified and kept but not queued: answering one writes nothing
            anywhere.
          </p>
        </Panel>
      )}

      <Panel
        title="Messages"
        subtitle="Everything here survived classification"
        actions={
          <div className="flex gap-1">
            {FILTERS.map((f) => (
              <Button key={f.value || "all"} tone={filter === f.value ? "info" : "neutral"} onClick={() => setFilter(f.value)}>
                {f.label}
              </Button>
            ))}
          </div>
        }
      >
        {messages.length === 0 ? (
          <Empty>
            {unclassified > 0
              ? "Nothing classified yet. Run Classify signals."
              : "No messages match this filter."}
          </Empty>
        ) : (
          <ul className="max-h-[30rem] divide-y divide-[var(--color-line)] overflow-y-auto">
            {messages.map((m) => (
              <li key={m.id} className="px-4 py-2.5">
                <div className="flex flex-wrap items-center gap-2 text-xs">
                  {m.classification ? (
                    <Badge tone={CLASS_TONE[m.classification as keyof typeof CLASS_TONE] ?? "neutral"}>
                      {m.classification}
                    </Badge>
                  ) : (
                    <Badge tone="warn">unclassified</Badge>
                  )}
                  <span className="font-mono text-[var(--color-muted)]">#{m.channel}</span>
                  <span className="text-[var(--color-muted)]">{m.author}</span>
                  {m.classification_confidence !== null && (
                    <span className="text-[var(--color-muted)]">
                      conf {m.classification_confidence.toFixed(2)}
                    </span>
                  )}
                </div>
                <p className="mt-1 text-sm">{m.text}</p>
              </li>
            ))}
          </ul>
        )}
      </Panel>
    </div>
  );
}
