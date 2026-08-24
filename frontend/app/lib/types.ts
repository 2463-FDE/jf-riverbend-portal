// Shared types mirroring the Riverbend gateway API contract.

export interface PortalUser {
  username: string;
  full_name: string;
  role: string;
}

export interface LoginResponse {
  token: string;
  user: PortalUser;
}

export interface PatientSummary {
  id: number;
  mrn: string;
  name: string;
  dob: string;
  gender: string;
  created_at: string;
}

export interface PatientListResponse {
  items: PatientSummary[];
  total: number;
  limit: number;
  offset: number;
}

export interface RecordItem {
  id: number;
  kind: string;
  body: string;
  // Lab-style records may carry structured result fields.
  test?: string;
  value?: string | number;
  unit?: string;
  reference_range?: string;
  status?: string; // normal | abnormal | high | low | ...
}

export interface EncounterBlock {
  encounter: {
    id: number;
    type: string;
    provider: string;
    summary: string;
    date?: string;
  };
  records: RecordItem[];
}

export interface RecordsResponse {
  patient_id: number;
  encounters: EncounterBlock[];
}

// Stage 3 — libs.patient_view_agent.PatientViewResult, served via
// GET /api/patients/[id]/view (gateway -> records-service, StaffAccessGate).
// This is a bounded, evidence-cited summary, NOT a fix for the /api/records
// IDOR above (see RIV-201) — see the frontend route's comment.
export interface PatientViewExecution {
  specialists_run: string[];
  tool_calls: number;
  reads: number;
  reads_complete: boolean;
  truncated: boolean;
  compose_attempts: number;
  elapsed_seconds: number;
}

export interface PatientViewResult {
  outcome: "completed" | "escalated" | "refused";
  summary: string;
  evidence_ids: string[];
  limitations: string[];
  escalation: boolean;
  reasons: string[];
  correlation_id: string;
  patient_id: number;
  execution: PatientViewExecution;
}

// Stage 2 (Week 6) — read-only "possible duplicate patient" reconciliation
// view, served via GET /api/patients/[id]/reconciliation (gateway ->
// records-service, StaffAccessGate — same authorization boundary as
// PatientViewResult above, NOT patient-specific; does not fix RIV-201).
// Exact-SSN-match only; allergies/medications are free text, not coded.
export interface IdentitySignal {
  signal_type: string; // "ssn_exact_match" is the only value this slice produces
  masked_value: string; // e.g. "•••-••-9981" — never the full ssn
}

export interface ReconciliationSourceRecord {
  patient_id: number;
  is_requested_patient: boolean;
  source_label: string; // "Current chart" | "Possible match"
  name_on_file: string;
  dob: string | null;
  allergies: string[];
  medications: string[];
}

export interface ReconciliationDiscrepancy {
  category: string; // "allergy" | "medication"
  value: string;
  present_on_patient_ids: number[];
  missing_on_patient_ids: number[];
  evidence_ids: string[];
  review_required: boolean;
}

export interface ReconciliationResult {
  patient_id: number;
  identity_signals: IdentitySignal[];
  source_records: ReconciliationSourceRecord[];
  discrepancies: ReconciliationDiscrepancy[];
  limitations: string[];
  escalation: boolean;
  correlation_id: string;
}

export interface Slot {
  id: number;
  provider: string;
  location: string;
  start_at: string;
  end_at: string;
  status: string;
}

export interface SlotsResponse {
  items: Slot[];
}

export interface Appointment {
  id: number;
  patient_id: number;
  provider: string;
  reason: string;
  location?: string;
  // w9-fixes P0 4.3: the backend's AppointmentOut field is `scheduled_for`,
  // not `start_at` (that name belongs to Slot above, a different shape) —
  // every appointment rendered here previously read undefined.
  scheduled_for?: string;
  status: string;
}

// Stage 2 (feature-readiness) — eligibility chat turn, served via
// POST /api/visits/[id]/messages (gateway -> eligibility-service). `id` is a
// real appointment id; the gateway verifies the caller is authorized for
// that appointment's patient before this ever reaches eligibility-service.
export interface VisitMessageResponse {
  visit_id: string;
  reply: string;
  tool_called: boolean;
  eligibility_status?: "active" | "inactive" | "unknown" | "pending" | "stale";
  termination_reason: "answered" | "max_turns" | "provider_error";
  turns_used: number;
}

// w-9-2-planner P1b — one line of the streaming counterpart's
// newline-delimited JSON body (POST /api/visits/[id]/messages/stream).
// "delta" carries a piece of final answer text; "done"/"error" is always
// exactly the last line, carrying safe categorical metadata only — never
// the reply text again (already streamed) except for "error", whose text
// is a fixed, safe message, never a raw provider error.
export interface VisitStreamEvent {
  kind: "delta" | "done" | "error";
  text?: string | null;
  tool_called?: boolean | null;
  eligibility_status?: "active" | "inactive" | "unknown" | "pending" | "stale" | null;
  termination_reason?: "answered" | "max_turns" | "provider_error" | null;
  turns_used?: number | null;
}

// Stage 3: async eligibility job lifecycle (services/eligibility-service/jobs.py).
export type EligibilityJobStatus =
  | "queued"
  | "running"
  | "succeeded"
  | "failed"
  | "retryable"
  | "dead_letter";

export interface EligibilityJobResponse {
  job_id: string;
  status: EligibilityJobStatus;
  retry_count: number;
  max_retries: number;
  manual_retry_count: number;
  max_manual_retries: number;
  result_status?: "active" | "inactive" | "unknown" | "pending" | "stale";
  result_checked_at?: string;
  error?: string; // exception TYPE name only, never a raw message
  created_at: string;
  updated_at: string;
}

export interface IntakeResponse {
  patient_id: number;
  elapsed_seconds: number;
  eligibility?: Record<string, unknown> | null;
  eligibility_status?: string | null;
  eligibility_job_id?: string | null;
}

// Stage 1 (feature-readiness) — plain-language explanation of one intake
// wizard step, served via POST /api/intake/instructions (gateway ->
// intake-service). No patient data is sent or returned; `step` is one of
// the four wizard steps in frontend/app/intake/page.tsx's STEPS.
export type IntakeInstructionsStep = "demographics" | "insurance" | "consents" | "review";

export interface IntakeInstructionsResponse {
  summary: string;
  used_fallback: boolean;
}

export interface RoiRequest {
  id: number;
  patient_id: number;
  recipient: string;
  recipient_type: string;
  purpose: string;
  date_range_start: string;
  date_range_end: string;
  status: string;
  created_at?: string;
}

// W9.2 — secure patient-clinician messaging.
export interface ThreadSummary {
  id: number;
  patient_id: number;
  patient_name: string | null;
  subject: string;
  status: "open" | "closed";
  last_sender_name: string | null;
  last_message_at: string | null;
  unread_count: number;
}

export interface ThreadMessage {
  id: number;
  thread_id: number;
  sender_user_id: number;
  sender_name: string;
  body: string;
  created_at: string;
}

export interface ThreadDetail {
  id: number;
  patient_id: number;
  patient_name: string | null;
  subject: string;
  status: "open" | "closed";
  created_at: string;
  messages: ThreadMessage[];
}

// W9.3 — Coverage & Eligibility workspace.
export interface CoverageItem {
  id: number;
  patient_id: number;
  payer_name: string | null;
  plan_type: string | null;
  group_number: string | null;
  member_id_masked: string | null;
  status: string | null;
  verified_at: string | null;
  has_member_id: boolean;
}

export interface EligibilityResult {
  category: string; // active | inactive | unknown | stale | pending | simulated | unavailable
  message?: string;
  can_retry?: boolean;
}

// w-9-2-planner P3 — policy navigator (read-only).
export interface PolicyCitation {
  citation_id: string;
  source_id: string;
  source_version: string;
  title: string;
  section_id: string;
}

export interface PolicyAnswer {
  answer: string;
  citations: PolicyCitation[];
  label: string; // "real" | "fixture" | "fallback"
  termination_reason: string; // "answered" | "no_evidence" | "provider_error" | "citation_invalid"
}
