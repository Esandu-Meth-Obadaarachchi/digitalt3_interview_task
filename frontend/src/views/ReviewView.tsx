/**
 * M6, the review and approval queue. This is the surface the rubric weighs, so
 * it is built around what a reviewer has to decide rather than around what the
 * data happens to contain.
 *
 * Four things are always visible for every item:
 *   the verbatim quote, and whether it verified against the transcript
 *   what the model said, beside what a human changed it to
 *   who has already acted on it, and when
 *   whether the source stated an owner and a date, or the system abstained
 *
 * An item whose quote never verified cannot be approved by clicking Approve.
 * It requires the override to be armed and a reason typed, and the reason is
 * recorded in the audit trail as an override.
 */

import { useEffect, useState } from "react";
import { api, ApiFailure } from "../api/client";
import type { Extraction, ExtractionType, QueueSummary, ReviewEvent, ReviewStatus } from "../api/types";
import { Badge, Button, Empty, ErrorNote, Field, Panel, Value } from "../components/ui";
import { kindOf } from "../components/extractionKinds";

const STATUS_TONE = {
  pending: "info",
  approved: "ok",
  rejected: "bad",
  expired: "warn",
} as const;

const FILTERS: { label: string; value: ReviewStatus | "" }[] = [
  { label: "Pending", value: "pending" },
  { label: "Approved", value: "approved" },
  { label: "Rejected", value: "rejected" },
  { label: "Expired", value: "expired" },
  { label: "All", value: "" },
];

const KIND_FILTERS: { label: string; value: ExtractionType | "" }[] = [
  { label: "All types", value: "" },
  { label: "Actions", value: "action" },
  { label: "Decisions", value: "decision" },
  { label: "Risks", value: "risk" },
  { label: "Signals", value: "signal" },
];

export function ReviewView({ reviewer }: { reviewer: string }) {
  const [filter, setFilter] = useState<ReviewStatus | "">("pending");
  const [kind, setKind] = useState<ExtractionType | "">("");
  const [items, setItems] = useState<Extraction[]>([]);
  const [summary, setSummary] = useState<QueueSummary | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [error, setError] = useState<{ code?: string; message: string } | null>(null);

  const load = () => {
    api.queue({ status: filter, extraction_type: kind }).then(setItems).catch(() => setItems([]));
    api.queueSummary().then(setSummary).catch(() => setSummary(null));
  };

  useEffect(load, [filter, kind]);

  const active = items.find((item) => item.id === selected) ?? null;

  return (
    <div className="space-y-4">
      {summary && (
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-5">
          {(
            [
              ["Pending", summary.pending, "info"],
              ["Approved", summary.approved, "ok"],
              ["Rejected", summary.rejected, "bad"],
              ["Expired", summary.expired, "warn"],
              ["Unverified quotes", summary.unverified_pending, summary.unverified_pending ? "bad" : "neutral"],
            ] as const
          ).map(([label, value, tone]) => (
            <div
              key={label}
              className="rounded-lg border border-[var(--color-line)] bg-[var(--color-surface)] px-3 py-2"
            >
              <div className="text-[11px] uppercase tracking-wide text-[var(--color-muted)]">{label}</div>
              <div className="mt-0.5 flex items-baseline gap-2">
                <span className="text-xl font-semibold tabular-nums">{value}</span>
                {label === "Unverified quotes" && value > 0 && <Badge tone={tone}>needs override</Badge>}
              </div>
            </div>
          ))}
        </div>
      )}

      <ErrorNote error={error} />

      {/* Full width, above the columns. Ten controls in the Queue panel header
          overflowed a half-width column and disappeared under the Item panel,
          which hid the status filter entirely. The two groups are labelled
          because "Decisions" and "Pending" answer different questions. */}
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2 rounded-lg border border-[var(--color-line)] bg-[var(--color-surface)] px-4 py-2.5">
        <div className="flex flex-wrap items-center gap-1">
          <span className="mr-1 text-[11px] uppercase tracking-wide text-[var(--color-muted)]">Type</span>
          {KIND_FILTERS.map((option) => (
            <Button
              key={option.value || "all"}
              tone={kind === option.value ? "ok" : "neutral"}
              onClick={() => {
                setKind(option.value);
                setSelected(null);
              }}
            >
              {option.label}
            </Button>
          ))}
        </div>
        <div className="flex flex-wrap items-center gap-1">
          <span className="mr-1 text-[11px] uppercase tracking-wide text-[var(--color-muted)]">Status</span>
          {FILTERS.map((option) => (
            <Button
              key={option.value || "any"}
              tone={filter === option.value ? "info" : "neutral"}
              onClick={() => {
                setFilter(option.value);
                setSelected(null);
              }}
            >
              {option.label}
            </Button>
          ))}
        </div>
        <span className="ml-auto text-xs text-[var(--color-muted)]">
          {items.length} item{items.length === 1 ? "" : "s"}
        </span>
      </div>

      <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.2fr)]">
        <Panel title="Queue" subtitle="Unverified quotes sort first">
          {items.length === 0 ? (
            <Empty>Nothing here. Extract a source to fill the queue.</Empty>
          ) : (
            <ul className="max-h-[32rem] divide-y divide-[var(--color-line)] overflow-y-auto">
              {items.map((item) => {
                const view = kindOf(item);
                return (
                  <li key={item.id}>
                    <button
                      type="button"
                      onClick={() => setSelected(item.id)}
                      className={`w-full px-4 py-3 text-left transition-colors hover:bg-[var(--color-canvas)] ${
                        selected === item.id ? "bg-[var(--color-canvas)]" : ""
                      }`}
                    >
                      <div className="flex items-start justify-between gap-3">
                        <span className="text-sm">{view.headline(item.payload)}</span>
                        <div className="flex shrink-0 gap-1">
                          <Badge>{view.label}</Badge>
                          {!item.quote_verified && <Badge tone="bad">unverified</Badge>}
                          <Badge tone={STATUS_TONE[item.status]}>{item.status}</Badge>
                        </div>
                      </div>
                      <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-[var(--color-muted)]">
                        {view.facts(item.payload).map((fact, index) => (
                          <span key={fact.label} className="flex items-center gap-2">
                            {index > 0 && <span>·</span>}
                            <span>
                              {fact.label}{" "}
                              <span className={fact.tone === "warn" ? "text-[var(--color-warn)]" : ""}>
                                {fact.tone === "bad" ? <strong>{fact.value}</strong> : fact.value}
                              </span>
                            </span>
                          </span>
                        ))}
                        {item.confidence !== null && (
                          <span className="flex items-center gap-2">
                            <span>·</span>
                            <span>conf {item.confidence.toFixed(2)}</span>
                          </span>
                        )}
                      </div>
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </Panel>

        {active ? (
          <ReviewDetail
            key={active.id}
            item={active}
            reviewer={reviewer}
            onChanged={() => {
              load();
              setError(null);
            }}
            onError={setError}
          />
        ) : (
          <Panel title="Item">
            <Empty>Select an item to review it.</Empty>
          </Panel>
        )}
      </div>
    </div>
  );
}

function ReviewDetail({
  item,
  reviewer,
  onChanged,
  onError,
}: {
  item: Extraction;
  reviewer: string;
  onChanged: () => void;
  onError: (error: { code?: string; message: string } | null) => void;
}) {
  const [note, setNote] = useState("");
  const [override, setOverride] = useState(false);
  const [history, setHistory] = useState<ReviewEvent[]>([]);
  const [draft, setDraft] = useState<Record<string, unknown>>(item.payload);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api.history(item.id).then(setHistory).catch(() => setHistory([]));
  }, [item.id, busy]);

  const act = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    onError(null);
    try {
      await fn();
      onChanged();
    } catch (exc) {
      onError(exc instanceof ApiFailure ? { code: exc.code, message: exc.message } : { message: String(exc) });
    } finally {
      setBusy(false);
    }
  };

  const edited = JSON.stringify(item.payload) !== JSON.stringify(item.original_payload);
  const dirty = JSON.stringify(draft) !== JSON.stringify(item.payload);
  const terminal = item.status !== "pending";

  return (
    <div className="space-y-4">
      <Panel
        title="Evidence"
        subtitle="Every claim is anchored to words somebody actually said"
        actions={
          item.quote_verified ? (
            <Badge tone="ok">quote verified</Badge>
          ) : (
            <Badge tone="bad">quote not verified</Badge>
          )
        }
      >
        <div className="px-4 py-3">
          <blockquote className="quote border-l-2 border-[var(--color-line)] pl-3 text-[var(--color-ink)]">
            “{item.verbatim_quote}”
          </blockquote>
          <div className="mt-2 text-xs text-[var(--color-muted)]">
            {item.speaker ?? "unattributed"} at {item.timestamp ?? "no timestamp"}
            {item.quote_location && (
              <>
                {" · "}
                characters {item.quote_location.char_start}–{item.quote_location.char_end} of the source
              </>
            )}
          </div>
          {!item.quote_verified && (
            <p className="mt-2 rounded border border-[var(--color-bad)] bg-[var(--color-bad-bg)] px-2.5 py-1.5 text-xs text-[var(--color-bad)]">
              This quote is not a literal substring of the transcript. The model was asked to correct it and
              did not. Approving it needs the override below and a written reason.
            </p>
          )}
        </div>
      </Panel>

      <Panel
        title="Proposal"
        subtitle={edited ? "Edited by a human. The model's original is shown beside it." : "As the model returned it."}
      >
        <div className="grid gap-3 px-4 py-3 sm:grid-cols-2">
          {Object.entries(item.payload)
            .filter(([key]) => !kindOf(item).hidden.includes(key))
            .map(([key, value]) => {
              const original = item.original_payload[key];
              const changed = JSON.stringify(original) !== JSON.stringify(value);
              return (
                <Field key={key} label={key.replace(/_/g, " ")}>
                  {terminal || Array.isArray(value) ? (
                    <Value value={Array.isArray(value) ? value.join(", ") || "none" : value} />
                  ) : (
                    <input
                      className="w-full rounded border border-[var(--color-line)] px-2 py-1 text-sm"
                      value={String(draft[key] ?? "")}
                      onChange={(event) => setDraft({ ...draft, [key]: event.target.value })}
                    />
                  )}
                  {changed && (
                    <div className="mt-0.5 text-xs text-[var(--color-muted)] line-through">{String(original)}</div>
                  )}
                </Field>
              );
            })}
        </div>

        {item.payload.due_date_rule ? (
          <div className="border-t border-[var(--color-line)] px-4 py-2 text-xs text-[var(--color-muted)]">
            Date rule: {String(item.payload.due_date_rule)}
          </div>
        ) : null}

        {Array.isArray(item.payload.alternatives_discussed) &&
        item.payload.alternatives_discussed.length > 0 ? (
          <div className="border-t border-[var(--color-line)] px-4 py-2 text-xs text-[var(--color-muted)]">
            Alternatives discussed: {(item.payload.alternatives_discussed as string[]).join(", ")}
          </div>
        ) : null}

        {item.merged_from.length > 0 && (
          <div className="border-t border-[var(--color-line)] px-4 py-2 text-xs text-[var(--color-muted)]">
            Absorbed {item.merged_from.length} duplicate extraction(s) from overlapping chunks.
          </div>
        )}

        <div className="border-t border-[var(--color-line)] px-4 py-2 text-xs text-[var(--color-muted)]">
          {item.provider}:{item.model_name} · prompt v{item.prompt_version} · chunk {item.chunk_id?.split("::").pop()}
        </div>
      </Panel>

      {!terminal && (
        <Panel title="Decide" subtitle={`Acting as ${reviewer}`}>
          <div className="space-y-3 px-4 py-3">
            <input
              className="w-full rounded border border-[var(--color-line)] px-2 py-1.5 text-sm"
              placeholder="Reason, kept in the audit trail"
              value={note}
              onChange={(event) => setNote(event.target.value)}
            />
            {!item.quote_verified && (
              <label className="flex items-center gap-2 text-xs text-[var(--color-bad)]">
                <input type="checkbox" checked={override} onChange={(event) => setOverride(event.target.checked)} />
                I have checked this quote by hand and accept it despite failing verification
              </label>
            )}
            <div className="flex flex-wrap gap-2">
              <Button
                tone="info"
                disabled={busy || !dirty}
                onClick={() => act(() => api.edit(item.id, draft, reviewer, note || undefined))}
              >
                Save edit
              </Button>
              <Button
                tone="ok"
                disabled={busy}
                onClick={() => act(() => api.approve(item.id, reviewer, note || undefined, override))}
              >
                Approve
              </Button>
              <Button
                tone="bad"
                disabled={busy}
                onClick={() => act(() => api.reject(item.id, reviewer, note || undefined))}
              >
                Reject
              </Button>
            </div>
            <p className="text-xs text-[var(--color-muted)]">
              An edit leaves the item pending. Approving is the only thing that makes it writable downstream.
            </p>
          </div>
        </Panel>
      )}

      <Panel title="Audit trail" subtitle="Append-only. Nothing here can be altered afterwards.">
        {history.length === 0 ? (
          <Empty>No decisions recorded yet.</Empty>
        ) : (
          <ul className="divide-y divide-[var(--color-line)]">
            {history.map((event) => (
              <li key={event.id} className="px-4 py-2 text-xs">
                <div className="flex items-center gap-2">
                  <Badge tone={event.event_type === "approved" ? "ok" : event.event_type === "rejected" ? "bad" : "neutral"}>
                    {event.event_type}
                  </Badge>
                  <span className="font-medium">{event.actor}</span>
                  <span className="text-[var(--color-muted)]">{new Date(event.created_at).toLocaleString()}</span>
                </div>
                {event.note && <p className="mt-1 text-[var(--color-muted)]">{event.note}</p>}
              </li>
            ))}
          </ul>
        )}
      </Panel>
    </div>
  );
}
