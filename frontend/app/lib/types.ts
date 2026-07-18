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
  start_at?: string;
  end_at?: string;
  status: string;
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
