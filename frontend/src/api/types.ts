/**
 * Types mirroring the backend's Pydantic contracts.
 *
 * Hand-written rather than generated: the surface is small, and a hand-written
 * type that is read alongside the Python contract is easier to keep honest
 * than a generation step nobody re-runs.
 */

export type SourceStatus = "ingested" | "refused" | "error";
export type ReviewStatus = "pending" | "approved" | "rejected" | "expired";
export type ExtractionType = "action" | "decision" | "risk" | "signal";
export type DefectSeverity = "error" | "warning";

export interface Health {
  status: string;
  schema_version: string | null;
  llm_provider: string;
  llm_model: string;
  llm_key_present: boolean;
  llm_available: boolean;
  llm_detail: string;
  retrieval_mode: string;
  tracker_provider: string;
}

export interface Source {
  id: string;
  title: string;
  source_type: string;
  meeting_date: string | null;
  participants: string[];
  consent_flag: boolean;
  origin_format: string | null;
  file_path: string | null;
  content_hash: string | null;
  ingested_at: string;
  status: SourceStatus;
  refusal_reason: string | null;
  error_detail: string | null;
}

export interface Defect {
  code: string;
  severity: DefectSeverity;
  detail: string;
  line_number: number | null;
  excerpt: string | null;
}

export interface ConsentDecision {
  source_id: string;
  granted: boolean;
  reason: string;
  checked_at: string;
}

export interface IngestionReport {
  source_id: string;
  ok: boolean;
  status: SourceStatus;
  consent: ConsentDecision | null;
  /** True when the file matched what is stored and nothing was rewritten. */
  unchanged: boolean;
  origin_format: string | null;
  encoding: string | null;
  bytes_read: number;
  content_hash: string | null;
  segments_parsed: number;
  messages_parsed: number;
  direct_messages_excluded: number;
  speakers: string[];
  silent_participants: string[];
  duration_seconds: number | null;
  defects: Defect[];
  rejection_reason: string | null;
}

export interface Segment {
  id: string;
  source_id: string;
  segment_index: number;
  speaker: string | null;
  start_ts: string | null;
  text: string;
  char_start: number;
  char_end: number;
}

export interface QuoteLocation {
  char_start: number;
  char_end: number;
  segment_id: string | null;
}

export interface Extraction {
  id: string;
  source_id: string;
  extraction_type: ExtractionType;
  payload: Record<string, unknown>;
  original_payload: Record<string, unknown>;
  verbatim_quote: string;
  quote_verified: boolean;
  quote_location: QuoteLocation | null;
  speaker: string | null;
  timestamp: string | null;
  confidence: number | null;
  dedup_key: string | null;
  chunk_id: string | null;
  merged_from: string[];
  provider: string | null;
  model_name: string | null;
  prompt_version: string | null;
  status: ReviewStatus;
  reviewer: string | null;
  reviewed_at: string | null;
  review_note: string | null;
  expires_at: string | null;
  created_at: string;
}

export interface ReviewEvent {
  id: string;
  extraction_id: string;
  event_type: "created" | "edited" | "approved" | "rejected" | "expired";
  actor: string;
  status_before: ReviewStatus | null;
  status_after: ReviewStatus | null;
  payload_before: Record<string, unknown> | null;
  payload_after: Record<string, unknown> | null;
  note: string | null;
  created_at: string;
}

export interface QueueSummary {
  pending: number;
  approved: number;
  rejected: number;
  expired: number;
  unverified_pending: number;
}

export interface ExtractionRun {
  source_id: string;
  extraction_type: ExtractionType;
  prompt_version: string;
  provider: string;
  model: string;
  chunks: number;
  candidates: number;
  duplicates_removed: number;
  stored: number;
  verified_quotes: number;
  unverified_quotes: number;
  unspecified_owner: number;
  unspecified_due_date: number;
  dates_resolved: number;
  failed_chunks: string[];
  duration_ms: number;
}

export interface ApiError {
  error: string;
  detail: string;
}

// --- M7 tracker ------------------------------------------------------------

export interface TrackerItem {
  external_ref: string;
  title: string;
  description: string | null;
  assignee: string | null;
  /** Free text, kept exactly as the tracker holds it, whitespace included. */
  status: string;
  due_date: string | null;
  labels: string[];
  /** Our extraction id, or null for the pre-existing backlog. */
  source_ref: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export type WriteOutcome = "created" | "deduplicated" | "blocked";

export interface WriteAttempt {
  id: string;
  extraction_id: string;
  outcome: WriteOutcome;
  provider: string;
  attempted_at: string;
  external_ref: string | null;
  reason: string | null;
}

export interface WriteResult {
  outcome: WriteOutcome;
  extraction_id: string;
  item: TrackerItem | null;
  reason: string | null;
}

export interface TrackerSummary {
  items_total: number;
  items_written_by_agent: number;
  items_pre_existing: number;
  audited_writes: number;
  attempts: Partial<Record<WriteOutcome, number>>;
}

// --- ingestion outcome and chunks -------------------------------------------

export interface IngestionOutcome {
  source: Source;
  report: IngestionReport;
  segments: Segment[];
}

/** Exactly what the model is sent. `context` is background and never quotable. */
export interface Chunk {
  id: string;
  source_id: string;
  index: number;
  total: number;
  segment_ids: string[];
  overlap_segment_ids: string[];
  first_segment_index: number;
  last_segment_index: number;
  start_ts: string | null;
  end_ts: string | null;
  char_start: number;
  char_end: number;
  text: string;
  context: string;
  estimated_tokens: number;
}

// --- retrieval ---------------------------------------------------------------

export interface IndexStats {
  vectors: number;
  dimensions: number;
  model: string;
  by_type: Record<string, number>;
  index_path: string | null;
  built_at: string | null;
}

export interface SearchHit {
  ref_type: string;
  ref_id: string;
  source_id: string;
  source_title: string | null;
  text: string;
  speaker: string | null;
  timestamp: string | null;
  char_start: number | null;
  char_end: number | null;
  score: number;
  keyword_rank: number | null;
  dense_rank: number | null;
  keyword_score: number | null;
  dense_score: number | null;
}

export interface Citation {
  source_id: string;
  source_title: string | null;
  segment_id: string | null;
  message_id: string | null;
  speaker: string | null;
  timestamp: string | null;
  quote: string;
  char_start: number | null;
  char_end: number | null;
}

export interface AnswerClaim {
  statement: string;
  citation: Citation;
  verified: boolean;
}

export interface Answer {
  question: string;
  found: boolean;
  answer: string;
  claims: AnswerClaim[];
  retrieval_mode: string;
  considered: SearchHit[];
  provider: string | null;
  model: string | null;
  prompt_version: string | null;
  dropped_claims: string[];
  duration_ms: number;
  answered_at: string;
}

// --- M9 chat signals ---------------------------------------------------------

export type SignalClass = "decision" | "blocker" | "question" | "request" | "noise";

export interface StoredMessage {
  id: string;
  source_id: string;
  channel: string;
  author: string;
  ts: string;
  text: string;
  thread_id: string | null;
  /** Never "noise": noise is discarded, and the schema will not store it. */
  classification: SignalClass | null;
  classification_confidence: number | null;
  classified_at: string | null;
}

export interface SignalRun {
  source_id: string;
  prompt_version: string;
  provider: string;
  model: string;
  batches: number;
  messages_seen: number;
  classified: number;
  noise_discarded: number;
  queued: number;
  by_class: Record<string, number>;
  failed_batches: string[];
  duration_ms: number;
}

export interface ChatSummary {
  by_class: Record<string, number>;
  channels: string[];
}

// --- M10 digests and the scheduler -------------------------------------------

export interface DigestLine {
  text: string;
  citation: Citation;
  extraction_id: string;
  extraction_type: string;
  /** Why this line is in this section, so a reader can disagree with the pick. */
  because: string;
}

export interface Digest {
  id: string;
  scope_type: string;
  scope_key: string;
  scope_title: string;
  digest_date: string;
  generated_at: string;
  trigger: string;
  moved: DigestLine[];
  attention: DigestLine[];
  to_decide: DigestLine[];
  considered: number;
}

export interface ScheduledJob {
  id: string;
  name: string;
  trigger: string;
  /** From APScheduler, not from configuration. */
  next_run_at: string | null;
  description: string;
}

export interface SchedulerStatus {
  running: boolean;
  enabled: boolean;
  timezone: string;
  jobs: ScheduledJob[];
}

export interface Notification {
  id: string;
  channel: string;
  subject: string;
  body: string;
  posted_at: string;
  provider: string;
}

// --- M11 outcome records -----------------------------------------------------

export interface OutcomeCitation {
  source_id: string;
  source_title: string | null;
  speaker: string | null;
  timestamp: string | null;
  quote: string;
  quote_verified: boolean;
}

export interface OutcomeItem {
  id: string;
  type: string;
  payload: Record<string, unknown>;
  citation: OutcomeCitation;
  confidence: number | null;
  approved_by: string | null;
  approved_at: string | null;
  edited_by_reviewer: boolean;
}

export interface OutcomeRecord {
  schema_version: string;
  record_version: number;
  record_id: string;
  source_id: string;
  source_title: string;
  source_type: string;
  meeting_date: string | null;
  participants: string[];
  consent_flag: boolean;
  generated_at: string;
  actions: OutcomeItem[];
  decisions: OutcomeItem[];
  risks: OutcomeItem[];
  signals: OutcomeItem[];
  pending_not_included: number;
  rejected_not_included: number;
  expired_not_included: number;
}

export interface OutcomeSummary {
  source_id: string;
  schema_version: string;
  record_version: number;
  consent_flag: number;
  file_path: string;
  created_at: string;
  actions: number;
  decisions: number;
  risks: number;
  signals: number;
}
