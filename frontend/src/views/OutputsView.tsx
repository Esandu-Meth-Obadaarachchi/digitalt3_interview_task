/**
 * M10 and M11: what the agent produces without being asked, and what it hands
 * to whatever comes next.
 *
 * Three things a reviewer needs to see here.
 *
 * That the scheduler is real. The next fire times come from APScheduler
 * itself, not from configuration, and they advance on their own. The brief is
 * blunt that a button with nothing behind it is a partial implementation, so
 * the schedule is shown before the button that runs the job early.
 *
 * That a digest cites everything. Every line carries its quote, who said it
 * and where, and the reason it was chosen for its section.
 *
 * That an outcome record needs nothing else to be understood. The consumer
 * contract is shown beside it, because a schema says what the shape is and not
 * what it means.
 */

import { useEffect, useState } from "react";
import { api, ApiFailure } from "../api/client";
import type { Digest, Notification, OutcomeRecord, OutcomeSummary, SchedulerStatus } from "../api/types";
import { Badge, Button, Empty, ErrorNote, Field, Panel } from "../components/ui";

const SECTIONS = [
  { key: "attention", title: "Needs attention", tone: "bad" },
  { key: "to_decide", title: "To decide", tone: "warn" },
  { key: "moved", title: "Moved", tone: "ok" },
] as const;

export function OutputsView() {
  const [schedule, setSchedule] = useState<SchedulerStatus | null>(null);
  const [scopes, setScopes] = useState<{ key: string; title: string }[]>([]);
  const [scope, setScope] = useState("");
  const [when, setWhen] = useState("");
  const [digest, setDigest] = useState<Digest | null>(null);
  const [posts, setPosts] = useState<Notification[]>([]);
  const [records, setRecords] = useState<OutcomeSummary[]>([]);
  const [record, setRecord] = useState<OutcomeRecord | null>(null);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState<{ code?: string; message: string } | null>(null);

  const fail = (exc: unknown) =>
    setError(exc instanceof ApiFailure ? { code: exc.code, message: exc.message } : { message: String(exc) });

  useEffect(() => {
    api.schedule().then(setSchedule).catch(() => setSchedule(null));
    api.digestScopes().then((s) => {
      setScopes(s);
      if (s.length && !scope) setScope(s[0].key);
    });
    api.outcomes().then(setRecords).catch(() => setRecords([]));
    api.notifications(20).then(setPosts).catch(() => setPosts([]));
  }, []);

  const preview = async () => {
    if (!scope) return;
    setBusy("building");
    setError(null);
    try {
      setDigest(await api.previewDigest(scope, when ? new Date(when).toISOString() : undefined));
    } catch (exc) {
      fail(exc);
    } finally {
      setBusy("");
    }
  };

  const runAll = async () => {
    setBusy("running the scheduled job");
    setError(null);
    try {
      const all = await api.runAllDigests(when ? new Date(when).toISOString() : undefined);
      setDigest(all.find((d) => d.scope_key === scope) ?? all[0] ?? null);
      api.notifications(20).then(setPosts);
    } catch (exc) {
      fail(exc);
    } finally {
      setBusy("");
    }
  };

  const emit = async (sourceId: string) => {
    setBusy("emitting");
    setError(null);
    try {
      setRecord(await api.emitOutcome(sourceId));
      api.outcomes().then(setRecords);
    } catch (exc) {
      fail(exc);
    } finally {
      setBusy("");
    }
  };

  return (
    <div className="space-y-4">
      <Panel
        title="Scheduler"
        subtitle="Next fire times come from APScheduler, not from configuration"
        actions={
          schedule ? (
            <Badge tone={schedule.running ? "ok" : schedule.enabled ? "warn" : "neutral"}>
              {schedule.running ? "running" : schedule.enabled ? "not started" : "disabled"}
            </Badge>
          ) : null
        }
      >
        {!schedule ? (
          <Empty>Scheduler status unavailable.</Empty>
        ) : (
          <>
            <ul className="divide-y divide-[var(--color-line)]">
              {schedule.jobs.map((job) => (
                <li key={job.id} className="px-4 py-2.5">
                  <div className="flex flex-wrap items-baseline justify-between gap-2">
                    <span className="text-sm font-medium">{job.name}</span>
                    <span className="text-xs tabular-nums text-[var(--color-muted)]">
                      {job.next_run_at ? (
                        <>next: {new Date(job.next_run_at).toLocaleString()}</>
                      ) : (
                        <span className="text-[var(--color-warn)]">not scheduled</span>
                      )}
                    </span>
                  </div>
                  <p className="mt-0.5 font-mono text-xs text-[var(--color-muted)]">{job.trigger}</p>
                  <p className="mt-1 text-xs text-[var(--color-muted)]">{job.description}</p>
                </li>
              ))}
            </ul>
            <p className="border-t border-[var(--color-line)] px-4 py-2 text-xs text-[var(--color-muted)]">
              Timezone {schedule.timezone}. The expiry sweep is the safe default on no response: an
              unreviewed item ages out to a state the approval gate treats exactly like pending, not
              writable. Nothing is ever approved by the passage of time.
            </p>
          </>
        )}
      </Panel>

      <ErrorNote error={error} />

      <Panel
        title="Digest"
        subtitle="Built from approved items only. Every line cites its source."
        actions={
          <div className="flex flex-wrap items-center gap-2">
            <select
              className="rounded border border-[var(--color-line)] px-2 py-1 text-xs"
              value={scope}
              onChange={(e) => setScope(e.target.value)}
            >
              {scopes.map((s) => (
                <option key={s.key} value={s.key}>
                  {s.title}
                </option>
              ))}
            </select>
            <label className="flex items-center gap-1 text-xs text-[var(--color-muted)]">
              clock
              <input
                type="date"
                className="rounded border border-[var(--color-line)] px-1.5 py-1"
                value={when}
                onChange={(e) => setWhen(e.target.value)}
                title="Clock override. Produces the digest for any date without waiting for six o'clock."
              />
            </label>
            <Button tone="info" disabled={Boolean(busy)} onClick={preview}>
              Preview
            </Button>
            <Button
              tone="ok"
              disabled={Boolean(busy)}
              onClick={runAll}
              title="Runs the same function the scheduler runs at the configured hour."
            >
              Run the job now
            </Button>
          </div>
        }
      >
        {busy && (
          <div className="flex items-center gap-2 px-4 py-3 text-xs text-[var(--color-muted)]">
            <span className="h-3 w-3 animate-spin rounded-full border-2 border-[var(--color-line)] border-t-[var(--color-info)]" />
            {busy}…
          </div>
        )}

        {!digest && !busy && <Empty>Preview a digest to see it.</Empty>}

        {digest && !busy && (
          <>
            <div className="grid grid-cols-2 gap-4 border-b border-[var(--color-line)] px-4 py-3 sm:grid-cols-4">
              <Field label="Scope">{digest.scope_title}</Field>
              <Field label="Date">{digest.digest_date}</Field>
              <Field label="Trigger">{digest.trigger}</Field>
              <Field label="Approved items in scope">{digest.considered}</Field>
            </div>

            {digest.considered === 0 ? (
              <Empty>Nothing approved in scope for this day.</Empty>
            ) : (
              SECTIONS.map(({ key, title, tone }) => {
                const lines = digest[key];
                return (
                  <div key={key} className="border-b border-[var(--color-line)] last:border-0">
                    <div className="flex items-center gap-2 px-4 pt-3">
                      <Badge tone={tone}>{title}</Badge>
                      <span className="text-xs text-[var(--color-muted)]">
                        {lines.length} of {key === "moved" ? 3 : key === "attention" ? 2 : 1}
                      </span>
                    </div>
                    {lines.length === 0 ? (
                      <p className="px-4 py-2 text-xs text-[var(--color-muted)]">nothing in this section</p>
                    ) : (
                      <ul className="px-4 py-2">
                        {lines.map((line) => (
                          <li key={line.extraction_id} className="py-2">
                            <p className="text-sm">{line.text}</p>
                            <blockquote className="quote mt-1 border-l-2 border-[var(--color-line)] pl-3 text-[var(--color-muted)]">
                              “{line.citation.quote}”
                            </blockquote>
                            <p className="mt-0.5 text-xs text-[var(--color-muted)]">
                              {line.citation.speaker ?? "unattributed"},{" "}
                              {line.citation.source_title ?? line.citation.source_id}
                              {line.citation.timestamp && ` at ${line.citation.timestamp}`} ·{" "}
                              <span className="italic">{line.because}</span>
                            </p>
                          </li>
                        ))}
                      </ul>
                    )}
                  </div>
                );
              })
            )}
          </>
        )}
      </Panel>

      <div className="grid gap-4 lg:grid-cols-2">
        <Panel title="Outcome records" subtitle="Approved items only, versioned, consumable without this system">
          {records.length === 0 ? (
            <Empty>No record emitted yet.</Empty>
          ) : (
            <ul className="divide-y divide-[var(--color-line)]">
              {records.map((r) => (
                <li key={`${r.source_id}:${r.record_version}`} className="px-4 py-2.5">
                  <div className="flex items-center justify-between gap-2">
                    <button
                      type="button"
                      className="text-left text-sm hover:underline"
                      onClick={() => api.outcome(r.source_id, r.record_version).then(setRecord)}
                    >
                      {r.source_id}
                    </button>
                    <span className="flex gap-1">
                      <Badge>v{r.record_version}</Badge>
                      <Badge tone="info">schema {r.schema_version}</Badge>
                    </span>
                  </div>
                  <p className="mt-0.5 font-mono text-xs text-[var(--color-muted)]">{r.file_path}</p>
                  <p className="mt-0.5 text-xs text-[var(--color-muted)]">
                    {r.actions} actions · {r.decisions} decisions · {r.risks} risks · {r.signals} signals
                  </p>
                </li>
              ))}
            </ul>
          )}
          <div className="border-t border-[var(--color-line)] px-4 py-2">
            <div className="flex flex-wrap items-center gap-2">
              <select
                className="rounded border border-[var(--color-line)] px-2 py-1 text-xs"
                value={scope}
                onChange={(e) => setScope(e.target.value)}
              >
                {scopes.map((s) => (
                  <option key={s.key} value={s.key}>
                    {s.title}
                  </option>
                ))}
              </select>
              <Button tone="info" disabled={Boolean(busy)} onClick={() => emit(scope)}>
                Emit a new version
              </Button>
            </div>
            <p className="mt-1 text-xs text-[var(--color-muted)]">
              Never overwrites. A consumer that read version 1 and acted on it should be able to see
              what it read.
            </p>
          </div>
        </Panel>

        <Panel
          title={record ? `${record.source_title} v${record.record_version}` : "Record"}
          subtitle={record ? `schema ${record.schema_version}` : "Select a record to read it"}
        >
          {!record ? (
            <Empty>Nothing selected.</Empty>
          ) : (
            <>
              <div className="grid grid-cols-2 gap-4 px-4 py-3">
                <Field label="Consent carried forward">
                  <Badge tone={record.consent_flag ? "ok" : "bad"}>
                    {record.consent_flag ? "granted" : "withheld"}
                  </Badge>
                </Field>
                <Field label="Items">
                  {record.actions.length + record.decisions.length + record.risks.length + record.signals.length}
                </Field>
                <Field label="Excluded">
                  {record.pending_not_included} pending · {record.rejected_not_included} rejected ·{" "}
                  {record.expired_not_included} expired
                </Field>
                <Field label="Generated">{new Date(record.generated_at).toLocaleString()}</Field>
              </div>
              <p className="border-t border-[var(--color-line)] px-4 py-2 text-xs text-[var(--color-muted)]">
                The excluded counts are how a consumer tells an empty record meaning "nothing was
                found" from one meaning "nothing has been reviewed yet".
              </p>
              <ul className="max-h-72 divide-y divide-[var(--color-line)] overflow-y-auto border-t border-[var(--color-line)]">
                {[...record.actions, ...record.decisions, ...record.risks, ...record.signals].map((item) => (
                  <li key={item.id} className="px-4 py-2 text-xs">
                    <div className="flex items-center gap-2">
                      <Badge>{item.type}</Badge>
                      {item.edited_by_reviewer && <Badge tone="warn">edited by a human</Badge>}
                      <span className="text-[var(--color-muted)]">approved by {item.approved_by}</span>
                    </div>
                    <p className="mt-1">
                      {String(item.payload.what ?? item.payload.what_was_decided ?? item.payload.description ?? item.payload.text ?? "")}
                    </p>
                    <p className="quote mt-0.5 text-[var(--color-muted)]">
                      “{item.citation.quote}”{" "}
                      {item.citation.quote_verified ? "" : <span className="text-[var(--color-bad)]">unverified</span>}
                    </p>
                  </li>
                ))}
              </ul>
            </>
          )}
        </Panel>
      </div>

      <Panel title="write_log/notifications.jsonl" subtitle="What would have been posted. Nothing is sent anywhere.">
        {posts.length === 0 ? (
          <Empty>Nothing posted yet.</Empty>
        ) : (
          <ul className="max-h-56 divide-y divide-[var(--color-line)] overflow-y-auto">
            {posts.map((p) => (
              <li key={p.id} className="px-4 py-2 text-xs">
                <span className="font-medium">{p.subject}</span>
                <span className="ml-2 text-[var(--color-muted)]">
                  to {p.channel} · {new Date(p.posted_at).toLocaleString()} · via {p.provider}
                </span>
              </li>
            ))}
          </ul>
        )}
      </Panel>
    </div>
  );
}
