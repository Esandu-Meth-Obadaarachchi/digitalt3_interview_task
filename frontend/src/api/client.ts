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
  Answer,
  ChatSummary,
  Chunk,
  Digest,
  EndOfDayResult,
  Extraction,
  ExtractionRun,
  ExtractionType,
  FollowUpDraft,
  Health,
  IndexStats,
  IngestionOutcome,
  IngestionReport,
  Notification,
  OutcomeRecord,
  OutcomeSummary,
  Person,
  PersonDigest,
  QueueSummary,
  ReviewEvent,
  ReviewStatus,
  SchedulerStatus,
  SearchHit,
  Segment,
  SignalRun,
  Source,
  StoredMessage,
  TrackerItem,
  TrackerSummary,
  WriteAttempt,
  WriteResult,
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

/** Multipart, so the browser sets its own boundary. Never send JSON headers here. */
async function upload<T>(path: string, form: FormData): Promise<T> {
  const response = await fetch(path, { method: "POST", body: form });
  if (!response.ok) {
    let code = String(response.status);
    let detail = response.statusText;
    try {
      const body = await response.json();
      code = body.error ?? code;
      detail = Array.isArray(body.detail)
        ? body.detail.map((d: { msg?: string }) => d.msg ?? String(d)).join("; ")
        : (body.detail ?? detail);
    } catch {
      /* keep the status text */
    }
    throw new ApiFailure(detail, code, response.status);
  }
  return (await response.json()) as T;
}

const post = <T>(path: string, body?: unknown) =>
  request<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });

const put = <T>(path: string, body?: unknown) =>
  request<T>(path, { method: "PUT", body: body === undefined ? undefined : JSON.stringify(body) });

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
  seed: () => post<IngestionOutcome[]>("/api/sources/seed"),
  chunks: (id: string) => request<Chunk[]>(`/api/sources/${encodeURIComponent(id)}/chunks`),

  uploadSource: (input: {
    file: File;
    source_id: string;
    title: string;
    consent_flag: boolean;
    /** transcript or chat_export. The same endpoint and the same consent gate
     *  serve both; only the parser differs. */
    source_type?: "transcript" | "audio" | "chat_export";
    meeting_date?: string;
    participants: string[];
  }) => {
    const form = new FormData();
    form.append("file", input.file);
    form.append("source_id", input.source_id);
    form.append("title", input.title);
    form.append("source_type", input.source_type ?? "transcript");
    // Sent as a string because the backend field is a bool and FormData has no
    // types. "true"/"false" is what Pydantic parses.
    form.append("consent_flag", String(input.consent_flag));
    if (input.meeting_date) form.append("meeting_date", input.meeting_date);
    form.append("participants", JSON.stringify(input.participants));
    return upload<IngestionOutcome>("/api/sources/upload", form);
  },

  // --- extraction ----------------------------------------------------------
  extractActions: (id: string) =>
    post<ExtractionRun>(`/api/extractions/${encodeURIComponent(id)}/actions`),
  extractAll: (id: string) =>
    post<ExtractionRun[]>(`/api/extractions/${encodeURIComponent(id)}/all`),

  // --- review --------------------------------------------------------------
  queue: (
    params: { status?: ReviewStatus | ""; source_id?: string; extraction_type?: ExtractionType | "" } = {},
  ) => request<Extraction[]>(`/api/review${query(params)}`),
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

  // --- tracker -------------------------------------------------------------
  trackerSummary: () => request<TrackerSummary>("/api/tracker/summary"),
  trackerItems: (params: { written_by_agent?: boolean; status?: string } = {}) =>
    request<TrackerItem[]>(`/api/tracker/items${query(params)}`),
  trackerAttempts: (extractionId?: string) =>
    request<WriteAttempt[]>(`/api/tracker/attempts${query({ extraction_id: extractionId })}`),
  trackerWriteLog: (limit?: number) =>
    request<Record<string, unknown>[]>(`/api/tracker/write-log${query({ limit })}`),
  trackerSync: () => post<WriteResult[]>("/api/tracker/sync"),

  // --- retrieval and question answering ------------------------------------
  indexStats: () => request<IndexStats>("/api/qa/index"),
  rebuildIndex: () => post<IndexStats>("/api/qa/index/rebuild"),
  retrieve: (question: string, mode?: string, limit?: number) =>
    post<SearchHit[]>("/api/qa/search", { question, mode, limit }),
  ask: (question: string, mode?: string, limit?: number) =>
    post<Answer>("/api/qa", { question, mode, limit }),

  // --- M9 chat signals ------------------------------------------------------
  chatMessages: (params: { source_id?: string; channel?: string; classification?: string } = {}) =>
    request<StoredMessage[]>(`/api/chat/messages${query(params)}`),
  chatSummary: (sourceId?: string) =>
    request<ChatSummary>(`/api/chat/summary${query({ source_id: sourceId })}`),
  classifySignals: (id: string) =>
    post<SignalRun>(`/api/chat/${encodeURIComponent(id)}/classify`),

  // --- M10 digests and the scheduler ----------------------------------------
  schedule: () => request<SchedulerStatus>("/api/digests/schedule"),
  digestScopes: () => request<{ key: string; title: string }[]>("/api/digests/scopes"),
  previewDigest: (scope: string, now?: string) =>
    request<Digest>(`/api/digests/${encodeURIComponent(scope)}${query({ now })}`),
  digestMarkdown: (scope: string, now?: string) =>
    request<{ scope_key: string; digest_date: string; markdown: string }>(
      `/api/digests/${encodeURIComponent(scope)}/markdown${query({ now })}`,
    ),
  runAllDigests: (now?: string) => post<EndOfDayResult>(`/api/digests/run/all${query({ now })}`),
  notifications: (limit?: number) =>
    request<Notification[]>(`/api/digests/posts/log${query({ limit })}`),

  // --- M13 per-person digests -----------------------------------------------
  people: () => request<Person[]>("/api/digests/people"),
  personDigest: (key: string, now?: string) =>
    request<PersonDigest>(`/api/digests/people/${encodeURIComponent(key)}${query({ now })}`),
  runAllPersonDigests: (now?: string) =>
    post<PersonDigest[]>(`/api/digests/people/run/all${query({ now })}`),

  // --- M12 follow-up drafts --------------------------------------------------
  followups: (sourceId?: string) =>
    request<FollowUpDraft[]>(`/api/followups${query({ source_id: sourceId })}`),
  previewFollowup: (sourceId: string) =>
    request<FollowUpDraft>(`/api/followups/preview/${encodeURIComponent(sourceId)}`),
  createFollowup: (sourceId: string) =>
    post<FollowUpDraft>(`/api/followups/${encodeURIComponent(sourceId)}`),
  editFollowup: (draftId: string, body: string, editedBy: string) =>
    put<FollowUpDraft>(`/api/followups/${encodeURIComponent(draftId)}`, { body, edited_by: editedBy }),
  /** sent_by has no default anywhere in the stack. A default would be the agent
   *  sending, which is the one thing M12 says must not happen. */
  sendFollowup: (draftId: string, sentBy: string, channel: string) =>
    post<FollowUpDraft>(`/api/followups/${encodeURIComponent(draftId)}/send`, { sent_by: sentBy, channel }),

  // --- M11 outcome records --------------------------------------------------
  outcomes: (sourceId?: string) =>
    request<OutcomeSummary[]>(`/api/outcomes${query({ source_id: sourceId })}`),
  outcome: (sourceId: string, version?: number) =>
    request<OutcomeRecord>(`/api/outcomes/${encodeURIComponent(sourceId)}${query({ version })}`),
  emitOutcome: (sourceId: string) =>
    post<OutcomeRecord>(`/api/outcomes/${encodeURIComponent(sourceId)}`),
};
