/**
 * M1 and M2 made visible.
 *
 * Refused and errored sources are listed alongside successful ones, because a
 * refusal is a record rather than an omission and the demo has to show the
 * non-consented meeting being seen and declined. The refused source's
 * "0 bytes read" is the evidence that its file was never opened.
 */

import { useEffect, useState } from "react";
import { api, ApiFailure } from "../api/client";
import type { ExtractionRun, IngestionReport, Source } from "../api/types";
import { Badge, Button, Empty, ErrorNote, Field, Panel } from "../components/ui";
import { UploadPanel } from "../components/UploadPanel";

const STATUS_TONE = { ingested: "ok", refused: "bad", error: "warn" } as const;

export function SourcesView({ onExtracted }: { onExtracted: () => void }) {
  const [sources, setSources] = useState<Source[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [report, setReport] = useState<IngestionReport | null>(null);
  const [runs, setRuns] = useState<ExtractionRun[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<{ code?: string; message: string } | null>(null);

  const load = () => api.sources().then(setSources).catch(fail);

  const fail = (exc: unknown) =>
    setError(
      exc instanceof ApiFailure
        ? { code: exc.code, message: exc.message }
        : { message: String(exc) },
    );

  useEffect(() => {
    load();
  }, []);

  useEffect(() => {
    setReport(null);
    setRuns([]);
    if (selected) api.report(selected).then(setReport).catch(() => setReport(null));
  }, [selected]);

  const seed = async () => {
    setBusy(true);
    setError(null);
    try {
      await api.seed();
      await load();
    } catch (exc) {
      fail(exc);
    } finally {
      setBusy(false);
    }
  };

  const extract = async (id: string) => {
    setBusy(true);
    setError(null);
    setRuns([]);
    try {
      // Actions, decisions and risks in one call. Each runs independently, so
      // one failing does not cost the work the others already did.
      setRuns(await api.extractAll(id));
      onExtracted();
    } catch (exc) {
      fail(exc);
    } finally {
      setBusy(false);
    }
  };

  const active = sources.find((s) => s.id === selected) ?? null;

  return (
    <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
      <div className="space-y-4">
        <UploadPanel
          onIngested={(result) => {
            load();
            setSelected(result.source.id);
          }}
        />

        <Panel
          title="Sources"
          subtitle={`${sources.length} ingested, refused or rejected`}
          actions={
            <Button
              onClick={seed}
              disabled={busy}
              title={
                "Re-runs ingestion over the four transcripts declared in " +
                "sample_data/metadata/sources.json. It does not reset the database, does not " +
                "load the tracker backlog, and does not extract anything. A file whose content " +
                "is unchanged is skipped entirely, so pressing this repeatedly is safe. " +
                "Use `make seed` for a full rebuild from schema.sql."
              }
            >
              {busy ? "working…" : "Re-ingest sample data"}
            </Button>
          }
        >
          {sources.length === 0 ? (
            <Empty>Nothing ingested yet. Seed the committed sample data to begin.</Empty>
          ) : (
            <ul className="divide-y divide-[var(--color-line)]">
              {sources.map((source) => (
                <li key={source.id}>
                  <button
                    type="button"
                    onClick={() => setSelected(source.id)}
                    className={`w-full px-4 py-3 text-left transition-colors hover:bg-[var(--color-canvas)] ${
                      selected === source.id ? "bg-[var(--color-canvas)]" : ""
                    }`}
                  >
                    <div className="flex items-center justify-between gap-3">
                      <span className="truncate text-sm font-medium">{source.title}</span>
                      <Badge tone={STATUS_TONE[source.status]}>{source.status}</Badge>
                    </div>
                    <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-[var(--color-muted)]">
                      <span>{source.meeting_date ?? "no date"}</span>
                      <span>·</span>
                      <span>{source.participants.length} participant(s)</span>
                      {!source.consent_flag && (
                        <>
                          <span>·</span>
                          <span className="font-medium text-[var(--color-bad)]">consent withheld</span>
                        </>
                      )}
                    </div>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </Panel>
        <ErrorNote error={error} />
      </div>

      <div className="space-y-4">
        {!active ? (
          <Panel title="Ingestion report">
            <Empty>Select a source to see what ingestion did with it.</Empty>
          </Panel>
        ) : (
          <>
            <Panel
              title={active.title}
              subtitle={active.id}
              actions={
                active.status === "ingested" ? (
                  <Button
                    tone="info"
                    disabled={busy}
                    onClick={() => extract(active.id)}
                    title="Runs M3 actions, M4 decisions and M5 risks. Each is independent, so one failing does not lose the others."
                  >
                    {busy ? "extracting…" : "Extract"}
                  </Button>
                ) : (
                  <Button
                    disabled
                    title={
                      active.status === "refused"
                        ? "Consent was withheld. Nothing may be sent to a model."
                        : "This source was rejected at ingestion and has no stored segments."
                    }
                  >
                    Extract
                  </Button>
                )
              }
            >
              <div className="grid grid-cols-2 gap-4 px-4 py-3 sm:grid-cols-3">
                <Field label="Status">
                  <Badge tone={STATUS_TONE[active.status]}>{active.status}</Badge>
                </Field>
                <Field label="Consent">
                  {active.consent_flag ? (
                    <Badge tone="ok">granted</Badge>
                  ) : (
                    <Badge tone="bad">withheld</Badge>
                  )}
                </Field>
                <Field label="Format">{active.origin_format ?? "not read"}</Field>
                <Field label="Bytes read">
                  <span className={report?.bytes_read === 0 ? "font-semibold text-[var(--color-bad)]" : ""}>
                    {report ? report.bytes_read.toLocaleString() : "—"}
                  </span>
                  {report?.unchanged && (
                    <span
                      className="ml-1 text-xs text-[var(--color-muted)]"
                      title="The file was byte-identical to what is stored, so nothing was rewritten and existing extractions and citations were left alone."
                    >
                      (unchanged)
                    </span>
                  )}
                </Field>
                <Field label="Segments">{report?.segments_parsed ?? "—"}</Field>
                <Field label="Speakers">{report?.speakers.length ?? "—"}</Field>
              </div>

              {(active.refusal_reason || active.error_detail) && (
                <div className="border-t border-[var(--color-line)] px-4 py-3">
                  <ErrorNote
                    error={{
                      code: active.status === "refused" ? "consent refused" : "rejected at ingestion",
                      message: active.refusal_reason ?? active.error_detail ?? "",
                    }}
                  />
                </div>
              )}

              {report?.silent_participants.length ? (
                <div className="border-t border-[var(--color-line)] px-4 py-2 text-xs text-[var(--color-muted)]">
                  Listed as present but never spoke: {report.silent_participants.join(", ")}
                </div>
              ) : null}
            </Panel>

            {report && report.defects.length > 0 && (
              <Panel title="Defects found" subtitle="Errors block ingestion. Warnings travel with the source.">
                <ul className="divide-y divide-[var(--color-line)]">
                  {report.defects.map((defect, index) => (
                    <li key={index} className="px-4 py-2.5">
                      <div className="flex items-center gap-2">
                        <Badge tone={defect.severity === "error" ? "bad" : "warn"}>{defect.severity}</Badge>
                        <span className="text-xs font-medium">{defect.code}</span>
                        {defect.line_number && (
                          <span className="text-xs text-[var(--color-muted)]">line {defect.line_number}</span>
                        )}
                      </div>
                      <p className="mt-1 text-xs text-[var(--color-muted)]">{defect.detail}</p>
                      {defect.excerpt && <p className="quote mt-1 text-[var(--color-muted)]">“{defect.excerpt}”</p>}
                    </li>
                  ))}
                </ul>
              </Panel>
            )}

            {runs.length > 0 && (
              <Panel
                title="Extraction runs"
                subtitle={`${runs[0].provider}:${runs[0].model} · ${runs.reduce((n, r) => n + r.chunks, 0)} chunk call(s)`}
              >
                <table className="w-full text-sm">
                  <thead>
                    <tr className="border-b border-[var(--color-line)] text-[11px] uppercase tracking-wide text-[var(--color-muted)]">
                      <th className="px-4 py-2 text-left font-medium">Type</th>
                      <th className="px-2 py-2 text-right font-medium">Found</th>
                      <th className="px-2 py-2 text-right font-medium">Deduped</th>
                      <th className="px-2 py-2 text-right font-medium">Stored</th>
                      <th className="px-2 py-2 text-right font-medium">Verified</th>
                      <th className="px-4 py-2 text-right font-medium">Prompt</th>
                    </tr>
                  </thead>
                  <tbody>
                    {runs.map((run) => (
                      <tr key={run.extraction_type} className="border-b border-[var(--color-line)] last:border-0">
                        <td className="px-4 py-2">{run.extraction_type}</td>
                        <td className="px-2 py-2 text-right tabular-nums">{run.candidates}</td>
                        <td className="px-2 py-2 text-right tabular-nums">{run.duplicates_removed}</td>
                        <td className="px-2 py-2 text-right tabular-nums font-medium">{run.stored}</td>
                        <td className="px-2 py-2 text-right tabular-nums">
                          <span className="text-[var(--color-ok)]">{run.verified_quotes}</span>
                          {run.unverified_quotes > 0 && (
                            <span className="text-[var(--color-bad)]"> +{run.unverified_quotes}?</span>
                          )}
                        </td>
                        <td className="px-4 py-2 text-right text-xs text-[var(--color-muted)]">
                          v{run.prompt_version}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                {runs.some((r) => r.failed_chunks.length > 0) && (
                  <div className="border-t border-[var(--color-line)] px-4 py-2 text-xs text-[var(--color-bad)]">
                    {runs.reduce((n, r) => n + r.failed_chunks.length, 0)} chunk(s) failed, so this run is
                    incomplete. Usually a rate limit or an exhausted free-tier quota.
                  </div>
                )}
              </Panel>
            )}
          </>
        )}
      </div>
    </div>
  );
}
