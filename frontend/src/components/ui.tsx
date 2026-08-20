/**
 * Small presentational pieces, kept in one file because there are few of them
 * and splitting six components across six files helps nobody.
 *
 * The only opinion encoded here: state always uses the same colour. Verified
 * and approved are green, unverified and rejected are red, pending and
 * abstention are amber. A reviewer scanning the queue should be able to read
 * state without reading words.
 */

import type { ReactNode } from "react";

type Tone = "ok" | "warn" | "bad" | "info" | "neutral";

const TONES: Record<Tone, string> = {
  ok: "bg-[var(--color-ok-bg)] text-[var(--color-ok)]",
  warn: "bg-[var(--color-warn-bg)] text-[var(--color-warn)]",
  bad: "bg-[var(--color-bad-bg)] text-[var(--color-bad)]",
  info: "bg-[var(--color-info-bg)] text-[var(--color-info)]",
  neutral: "bg-[var(--color-canvas)] text-[var(--color-muted)]",
};

export function Badge({ tone = "neutral", children }: { tone?: Tone; children: ReactNode }) {
  return (
    <span
      className={`inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[11px] font-medium uppercase tracking-wide ${TONES[tone]}`}
    >
      {children}
    </span>
  );
}

export function Panel({
  title,
  subtitle,
  actions,
  children,
}: {
  title?: ReactNode;
  subtitle?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="rounded-lg border border-[var(--color-line)] bg-[var(--color-surface)]">
      {(title || actions) && (
        <header className="flex items-start justify-between gap-4 border-b border-[var(--color-line)] px-4 py-3">
          <div>
            {title && <h2 className="text-sm font-semibold">{title}</h2>}
            {subtitle && <p className="mt-0.5 text-xs text-[var(--color-muted)]">{subtitle}</p>}
          </div>
          {actions && <div className="flex shrink-0 items-center gap-2">{actions}</div>}
        </header>
      )}
      {children}
    </section>
  );
}

export function Button({
  tone = "neutral",
  disabled,
  onClick,
  children,
  title,
}: {
  tone?: Tone;
  disabled?: boolean;
  onClick?: () => void;
  children: ReactNode;
  title?: string;
}) {
  const styles: Record<Tone, string> = {
    ok: "border-[var(--color-ok)] text-[var(--color-ok)] hover:bg-[var(--color-ok-bg)]",
    bad: "border-[var(--color-bad)] text-[var(--color-bad)] hover:bg-[var(--color-bad-bg)]",
    info: "border-[var(--color-info)] text-[var(--color-info)] hover:bg-[var(--color-info-bg)]",
    warn: "border-[var(--color-warn)] text-[var(--color-warn)] hover:bg-[var(--color-warn-bg)]",
    neutral: "border-[var(--color-line)] text-[var(--color-ink)] hover:bg-[var(--color-canvas)]",
  };
  return (
    <button
      type="button"
      title={title}
      disabled={disabled}
      onClick={onClick}
      className={`rounded border px-2.5 py-1 text-xs font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-40 ${styles[tone]}`}
    >
      {children}
    </button>
  );
}

export function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <div className="text-[11px] uppercase tracking-wide text-[var(--color-muted)]">{label}</div>
      <div className="mt-0.5 text-sm">{children}</div>
    </div>
  );
}

/** UNSPECIFIED is rendered as a deliberate answer, never as missing data. */
export function Value({ value }: { value: unknown }) {
  const text = value === null || value === undefined || value === "" ? "—" : String(value);
  if (text === "UNSPECIFIED") {
    return (
      <span
        className="text-[var(--color-warn)]"
        title="The source did not state this. The system abstains rather than guessing."
      >
        UNSPECIFIED
      </span>
    );
  }
  return <span>{text}</span>;
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="px-4 py-8 text-center text-sm text-[var(--color-muted)]">{children}</p>;
}

export function ErrorNote({ error }: { error: { code?: string; message: string } | null }) {
  if (!error) return null;
  return (
    <div className="rounded border border-[var(--color-bad)] bg-[var(--color-bad-bg)] px-3 py-2 text-xs text-[var(--color-bad)]">
      {error.code && <span className="font-semibold">{error.code}: </span>}
      {error.message}
    </div>
  );
}
