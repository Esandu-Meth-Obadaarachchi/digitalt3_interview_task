/**
 * Uploading a source, of either kind.
 *
 * A meeting transcript and a chat export take the same route: one endpoint,
 * one consent gate, different parsers. The choice is the first control on the
 * form rather than a guess made from the file extension, because a .json file
 * is a perfectly good transcript and guessing wrong would route private
 * channel messages through the wrong parser.
 *
 * The consent checkbox starts unchecked and the form cannot be submitted
 * without a decision being made about it. That is the whole reason this
 * component is more than a file input: consent is a property of the source,
 * the system refuses to touch anything that has not declared one, and a
 * default of "yes" would quietly make that guarantee meaningless.
 *
 * Uploading without consent is allowed on purpose. It is worth seeing: the
 * file is written, the gate refuses it, the file is deleted again, and the
 * report comes back with zero bytes read.
 */

import { useRef, useState } from "react";
import { api, ApiFailure } from "../api/client";
import type { IngestionOutcome } from "../api/types";
import { Badge, Button, ErrorNote, Field, Panel } from "./ui";

type Kind = "transcript" | "chat_export";

const ACCEPTED: Record<Kind, string> = {
  transcript: ".txt,.vtt,.json,.md,.log",
  chat_export: ".json",
};

const KINDS: { value: Kind; label: string; hint: string }[] = [
  { value: "transcript", label: "Meeting transcript", hint: "txt, vtt or json. The format is detected from the content." },
  { value: "chat_export", label: "Channel chat export", hint: "One json file holding every channel. Direct messages are dropped at ingestion." },
];

function slugify(name: string): string {
  return name
    .replace(/\.[^.]+$/, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-|-$/g, "")
    .slice(0, 48);
}

export function UploadPanel({ onIngested }: { onIngested: (outcome: IngestionOutcome) => void }) {
  const [kind, setKind] = useState<Kind>("transcript");
  const [file, setFile] = useState<File | null>(null);
  const [sourceId, setSourceId] = useState("");
  const [title, setTitle] = useState("");
  const [meetingDate, setMeetingDate] = useState("");
  const [participants, setParticipants] = useState("");
  const [consent, setConsent] = useState<boolean | null>(null);
  const [busy, setBusy] = useState(false);
  const [stage, setStage] = useState("");
  const [outcome, setOutcome] = useState<IngestionOutcome | null>(null);
  const [error, setError] = useState<{ code?: string; message: string } | null>(null);
  const [dragging, setDragging] = useState(false);
  const input = useRef<HTMLInputElement>(null);

  const take = (chosen: File | null) => {
    setFile(chosen);
    setOutcome(null);
    setError(null);
    if (chosen) {
      const slug = slugify(chosen.name);
      const today = new Date().toISOString().slice(0, 10);
      const prefix = kind === "chat_export" ? "chat" : "meeting";
      if (!sourceId) setSourceId(`${prefix}-${slug}-${today}`);
      if (!title) setTitle(chosen.name.replace(/\.[^.]+$/, "").replace(/[-_]+/g, " "));
    }
  };

  const submit = async () => {
    if (!file || consent === null) return;
    setBusy(true);
    setError(null);
    setOutcome(null);
    try {
      setStage("uploading the file");
      const result = await api.uploadSource({
        file,
        source_id: sourceId.trim(),
        title: title.trim() || file.name,
        consent_flag: consent,
        source_type: kind,
        meeting_date: meetingDate || undefined,
        participants: participants
          .split(",")
          .map((p) => p.trim())
          .filter(Boolean),
      });
      setStage("");
      setOutcome(result);
      onIngested(result);
    } catch (exc) {
      setStage("");
      setError(exc instanceof ApiFailure ? { code: exc.code, message: exc.message } : { message: String(exc) });
    } finally {
      setBusy(false);
    }
  };

  const ready = Boolean(file) && sourceId.trim().length > 0 && consent !== null && !busy;

  return (
    <Panel
      title="Upload a source"
      subtitle={KINDS.find((k) => k.value === kind)!.hint}
      actions={
        <div className="flex gap-1" role="group" aria-label="What kind of source is this">
          {KINDS.map((option) => (
            <Button
              key={option.value}
              tone={kind === option.value ? "info" : "neutral"}
              onClick={() => {
                setKind(option.value);
                setOutcome(null);
                setError(null);
              }}
            >
              {option.label}
            </Button>
          ))}
        </div>
      }
    >
      <div className="space-y-3 px-4 py-3">
        <div
          onDragOver={(e) => {
            e.preventDefault();
            setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={(e) => {
            e.preventDefault();
            setDragging(false);
            take(e.dataTransfer.files?.[0] ?? null);
          }}
          onClick={() => input.current?.click()}
          className={`cursor-pointer rounded border-2 border-dashed px-4 py-6 text-center text-sm transition-colors ${
            dragging
              ? "border-[var(--color-info)] bg-[var(--color-info-bg)]"
              : "border-[var(--color-line)] hover:bg-[var(--color-canvas)]"
          }`}
        >
          <input
            ref={input}
            type="file"
            accept={ACCEPTED[kind]}
            className="hidden"
            onChange={(e) => take(e.target.files?.[0] ?? null)}
          />
          {file ? (
            <span>
              <strong>{file.name}</strong>{" "}
              <span className="text-[var(--color-muted)]">({file.size.toLocaleString()} bytes)</span>
            </span>
          ) : (
            <span className="text-[var(--color-muted)]">
              {kind === "chat_export"
                ? "Drop a channel export here, or click to choose one"
                : "Drop a transcript here, or click to choose one"}
            </span>
          )}
        </div>

        <div className="grid gap-3 sm:grid-cols-2">
          <label className="block">
            <span className="text-[11px] uppercase tracking-wide text-[var(--color-muted)]">Source id</span>
            <input
              className="mt-0.5 w-full rounded border border-[var(--color-line)] px-2 py-1 text-sm"
              value={sourceId}
              onChange={(e) => setSourceId(e.target.value)}
              placeholder={kind === "chat_export" ? "chat-team-2026-08-22" : "meeting-something-2026-08-21"}
            />
          </label>
          <label className="block">
            <span className="text-[11px] uppercase tracking-wide text-[var(--color-muted)]">Title</span>
            <input
              className="mt-0.5 w-full rounded border border-[var(--color-line)] px-2 py-1 text-sm"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
            />
          </label>
          <label className="block">
            <span className="text-[11px] uppercase tracking-wide text-[var(--color-muted)]">
              {kind === "chat_export" ? "Export date" : "Meeting date"}{" "}
              <span className="normal-case">
                {kind === "chat_export" ? "(when the export was taken)" : "(anchors every relative date)"}
              </span>
            </span>
            <input
              type="date"
              className="mt-0.5 w-full rounded border border-[var(--color-line)] px-2 py-1 text-sm"
              value={meetingDate}
              onChange={(e) => setMeetingDate(e.target.value)}
            />
          </label>
          <label className="block">
            <span className="text-[11px] uppercase tracking-wide text-[var(--color-muted)]">
              {kind === "chat_export" ? "Channel members" : "Participants"}{" "}
              <span className="normal-case">(comma separated)</span>
            </span>
            <input
              className="mt-0.5 w-full rounded border border-[var(--color-line)] px-2 py-1 text-sm"
              value={participants}
              onChange={(e) => setParticipants(e.target.value)}
              placeholder="Sarah Chen, James Liu"
            />
          </label>
        </div>

        {/* No default. A source that has not declared consent is not processed,
            and pre-ticking this would quietly make that guarantee meaningless. */}
        <fieldset className="rounded border border-[var(--color-line)] px-3 py-2">
          <legend className="px-1 text-[11px] uppercase tracking-wide text-[var(--color-muted)]">
            Consent to process
          </legend>
          <div className="flex flex-wrap gap-4 text-sm">
            <label className="flex items-center gap-1.5">
              <input type="radio" checked={consent === true} onChange={() => setConsent(true)} />
              Granted
            </label>
            <label className="flex items-center gap-1.5">
              <input type="radio" checked={consent === false} onChange={() => setConsent(false)} />
              Withheld
            </label>
          </div>
          <p className="mt-1 text-xs text-[var(--color-muted)]">
            {consent === false
              ? "The file will be written, refused before it is opened, and deleted again. Worth watching."
              : "No default. Nothing is processed until this is answered."}
          </p>
        </fieldset>

        <div className="flex items-center gap-3">
          <Button tone="info" disabled={!ready} onClick={submit}>
            {busy ? "working…" : "Upload and ingest"}
          </Button>
          {busy && (
            <span className="flex items-center gap-2 text-xs text-[var(--color-muted)]">
              <span className="h-3 w-3 animate-spin rounded-full border-2 border-[var(--color-line)] border-t-[var(--color-info)]" />
              {stage}
            </span>
          )}
          {file && !busy && (
            <Button onClick={() => { take(null); setOutcome(null); }}>Clear</Button>
          )}
        </div>

        <ErrorNote error={error} />

        {outcome && (
          <div className="rounded border border-[var(--color-line)] bg-[var(--color-canvas)] px-3 py-2">
            <div className="flex items-center gap-2">
              <Badge
                tone={
                  outcome.source.status === "ingested"
                    ? "ok"
                    : outcome.source.status === "refused"
                      ? "bad"
                      : "warn"
                }
              >
                {outcome.source.status}
              </Badge>
              <span className="text-sm font-medium">{outcome.source.title}</span>
            </div>
            <div className="mt-2 grid grid-cols-3 gap-3">
              <Field label="Bytes read">
                <span className={outcome.report.bytes_read === 0 ? "font-semibold text-[var(--color-bad)]" : ""}>
                  {outcome.report.bytes_read.toLocaleString()}
                </span>
              </Field>
              {outcome.source.source_type === "chat_export" ? (
                <>
                  <Field label="Messages">{outcome.report.messages_parsed}</Field>
                  {/* The count is the only trace a direct message was ever seen,
                      so it is shown rather than buried in the report. */}
                  <Field label="Direct messages dropped">
                    <span className="font-semibold text-[var(--color-warn)]">
                      {outcome.report.direct_messages_excluded}
                    </span>
                  </Field>
                </>
              ) : (
                <>
                  <Field label="Segments">{outcome.report.segments_parsed}</Field>
                  <Field label="Format">{outcome.report.origin_format ?? "not read"}</Field>
                </>
              )}
            </div>
            {outcome.source.source_type === "chat_export" && outcome.source.status === "ingested" && (
              <p className="mt-2 text-xs text-[var(--color-muted)]">
                Classify the messages on the Channels tab. Ingestion needs no model, so this much works
                with no key and no quota.
              </p>
            )}
            {(outcome.source.refusal_reason || outcome.source.error_detail) && (
              <p className="mt-2 text-xs text-[var(--color-bad)]">
                {outcome.source.refusal_reason ?? outcome.source.error_detail}
              </p>
            )}
          </div>
        )}
      </div>
    </Panel>
  );
}
