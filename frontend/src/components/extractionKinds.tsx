/**
 * How each extraction type is read on screen.
 *
 * An action, a decision and a risk are not the same object with a different
 * label. They carry different fields and a reviewer checks different things, so
 * the queue asks each type what its headline is and what belongs beside it
 * rather than reaching for `payload.what` and rendering blanks for two thirds
 * of the queue.
 */

import type { Extraction, ExtractionType } from "../api/types";

type Tone = "ok" | "warn" | "bad" | "info" | "neutral";

export interface KindView {
  label: string;
  /** The one line that identifies this item in a list. */
  headline: (payload: Record<string, unknown>) => string;
  /** The facts a reviewer scans before opening it. */
  facts: (payload: Record<string, unknown>) => { label: string; value: string; tone?: Tone }[];
  /** Fields hidden from the editor because they are provenance, not content. */
  hidden: string[];
}

const str = (value: unknown, fallback = "—") =>
  value === null || value === undefined || value === "" ? fallback : String(value);

const SEVERITY_TONE: Record<string, Tone> = { high: "bad", medium: "warn", low: "neutral" };

// A blocker stops work, a request asks for it, a decision settles something.
// Coloured so a reviewer can read the queue without reading every word.
const SIGNAL_TONE: Record<string, Tone> = {
  blocker: "bad",
  request: "warn",
  decision: "ok",
  question: "info",
};

export const KINDS: Record<ExtractionType, KindView> = {
  action: {
    label: "Action",
    headline: (p) => str(p.what),
    facts: (p) => [
      { label: "owner", value: str(p.owner), tone: p.owner === "UNSPECIFIED" ? "warn" : undefined },
      { label: "due", value: str(p.due_date), tone: p.due_date === "UNSPECIFIED" ? "warn" : undefined },
    ],
    // due_date_type, due_date_stated and due_date_rule are how a date is
    // defended, not something a reviewer retypes.
    hidden: ["due_date_type", "due_date_rule"],
  },
  decision: {
    label: "Decision",
    headline: (p) => str(p.what_was_decided),
    facts: (p) => [
      { label: "stated by", value: str(p.who_stated_it), tone: p.who_stated_it === "UNSPECIFIED" ? "warn" : undefined },
      {
        label: "rationale",
        value: str(p.stated_rationale),
        tone: p.stated_rationale === "UNSPECIFIED" ? "warn" : undefined,
      },
    ],
    hidden: ["alternatives_discussed"],
  },
  risk: {
    label: "Risk",
    headline: (p) => str(p.description),
    facts: (p) => [
      { label: "severity", value: str(p.severity), tone: SEVERITY_TONE[String(p.severity)] ?? "neutral" },
      { label: "area", value: str(p.affected_area), tone: p.affected_area === "UNSPECIFIED" ? "warn" : undefined },
      { label: "owner", value: str(p.owner), tone: p.owner === "UNSPECIFIED" ? "warn" : undefined },
    ],
    hidden: [],
  },
  signal: {
    label: "Signal",
    headline: (p) => str(p.text),
    facts: (p) => [
      { label: "class", value: str(p.classification), tone: SIGNAL_TONE[String(p.classification)] },
      { label: "channel", value: `#${str(p.channel)}` },
      { label: "from", value: str(p.author) },
    ],
    // message_id is the link back to the message, not something to retype.
    hidden: ["message_id"],
  },
};

export const kindOf = (item: Extraction): KindView => KINDS[item.extraction_type] ?? KINDS.action;
