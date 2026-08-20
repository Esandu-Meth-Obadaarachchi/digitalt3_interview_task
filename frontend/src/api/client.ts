/**
 * The one place the frontend talks to the backend.
 *
 * Errors are unwrapped here so every caller sees an Error carrying the
 * backend's own message. That matters for this application specifically: a
 * refused approval and a refused consent are not failures of the interface,
 * they are the system working, and the reason has to reach the reviewer
 * verbatim rather than becoming "something went wrong".
 */

import type {
  Extraction,
  ExtractionRun,
  Health,
  IngestionReport,
  QueueSummary,
  ReviewEvent,
  ReviewStatus,
  Segment,
  Source,
} from "./types";

export class ApiFailure extends Error {
  // Declared as plain fields rather than constructor parameter properties:
  // TypeScript 6 builds with erasableSyntaxOnly, which forbids the shorthand.
  readonly code: string;
  readonly status: number;

  constructor(message: string, code: string, status: number) {
    super(message);
    this.name = "ApiFailure";
    this.code = code;
    this.status = status;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });

  if (!response.ok) {
    let code = String(response.status);
    let detail = response.statusText;
    try {
      const body = await response.json();
      code = body.error ?? code;
      detail = body.detail ?? body.message ?? JSON.stringify(body);
    } catch {
      /* a non-JSON error body stays as the status text */
    }
    throw new ApiFailure(detail, code, response.status);
  }

  return response.status === 204 ? (undefined as T) : ((await response.json()) as T);
}

const post = <T>(path: string, body?: unknown) =>
  request<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });

function query(params: Record<string, string | number | boolean | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== "") search.set(key, String(value));
  }
  const rendered = search.toString();
  return rendered ? `?${rendered}` : "";
}

export const api = {
  health: () => request<Health>("/health"),

  // --- sources -------------------------------------------------------------
  sources: () => request<Source[]>("/api/sources"),
  source: (id: string) => request<Source>(`/api/sources/${encodeURIComponent(id)}`),
  segments: (id: string) => request<Segment[]>(`/api/sources/${encodeURIComponent(id)}/segments`),
  report: (id: string) => request<IngestionReport>(`/api/sources/${encodeURIComponent(id)}/report`),
  sourceText: (id: string) =>
    request<{ source_id: string; length: number; text: string }>(
      `/api/sources/${encodeURIComponent(id)}/text`,
    ),
  seed: () => post<unknown[]>("/api/sources/seed"),

  // --- extraction ----------------------------------------------------------
  extractActions: (id: string) =>
    post<ExtractionRun>(`/api/extractions/${encodeURIComponent(id)}/actions`),

  // --- review --------------------------------------------------------------
  queue: (params: { status?: ReviewStatus | ""; source_id?: string } = {}) =>
    request<Extraction[]>(`/api/review${query(params)}`),
  queueSummary: (sourceId?: string) =>
    request<QueueSummary>(`/api/review/summary${query({ source_id: sourceId })}`),
  history: (id: string) => request<ReviewEvent[]>(`/api/review/${encodeURIComponent(id)}/history`),
  edit: (id: string, payload: Record<string, unknown>, reviewer: string, note?: string) =>
    post<Extraction>(`/api/review/${encodeURIComponent(id)}/edit`, { payload, reviewer, note }),
  approve: (id: string, reviewer: string, note?: string, overrideUnverified = false) =>
    post<Extraction>(`/api/review/${encodeURIComponent(id)}/approve`, {
      reviewer,
      note,
      override_unverified: overrideUnverified,
    }),
  reject: (id: string, reviewer: string, note?: string) =>
    post<Extraction>(`/api/review/${encodeURIComponent(id)}/reject`, { reviewer, note }),
};
