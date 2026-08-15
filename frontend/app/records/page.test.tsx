import { describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import RecordsPage from "./page";
import type { PatientViewResult, ReconciliationResult } from "../lib/types";

// Neither /patients/{id}/records, /patients/{id}/view, nor
// /patients/{id}/reconciliation are patient-scoped server-side (RIV-201) —
// so the client clearing stale panels the instant the Patient ID changes,
// and dropping any in-flight response for an id the user has moved on
// from, is the only thing standing between a clinician and reading one
// patient's chart under a different patient's heading. These tests pin
// that behavior down directly against frontend/app/records/page.tsx.

vi.mock("../lib/session", () => ({
  apiFetch: vi.fn(),
}));

import { apiFetch } from "../lib/session";

function jsonResponse(body: unknown, ok = true): Response {
  return { ok, json: async () => body } as Response;
}

function reconciliationFor(patientId: number, name: string): ReconciliationResult {
  return {
    patient_id: patientId,
    identity_signals: [{ signal_type: "ssn_exact_match", masked_value: "•••-••-9981" }],
    source_records: [
      {
        patient_id: patientId,
        is_requested_patient: true,
        source_label: "Current chart",
        name_on_file: name,
        dob: "1980-01-01",
        allergies: [],
        medications: [],
      },
      {
        patient_id: patientId + 1000,
        is_requested_patient: false,
        source_label: "Possible match",
        name_on_file: `${name} (possible match)`,
        dob: "1980-01-01",
        allergies: [],
        medications: [],
      },
    ],
    discrepancies: [],
    limitations: [],
    escalation: true,
    correlation_id: "corr-1",
  };
}

function aiViewFor(patientId: number, summary: string): PatientViewResult {
  return {
    outcome: "completed",
    summary,
    evidence_ids: [],
    limitations: [],
    escalation: false,
    reasons: [],
    correlation_id: "corr-2",
    patient_id: patientId,
    execution: {
      specialists_run: [],
      tool_calls: 0,
      reads: 0,
      reads_complete: true,
      truncated: false,
      compose_attempts: 1,
      elapsed_seconds: 0.1,
    },
  };
}

describe("RecordsPage — stale patient panel regression", () => {
  it("shows a safe existing-patient confirmation with DOB in MM/DD/YY and a masked SSN", async () => {
    vi.mocked(apiFetch).mockResolvedValueOnce(
      jsonResponse(reconciliationFor(1042, "Maria Gonzalez"))
    );

    render(<RecordsPage />);

    fireEvent.click(screen.getByRole("button", { name: /check for related records/i }));

    await waitFor(() => expect(screen.getByText(/confirm existing patient information/i)).toBeInTheDocument());
    expect(screen.getByText(/date of birth:/i).parentElement).toHaveTextContent("01/01/80");
    expect(screen.getByText(/^ssn:$/i).parentElement).toHaveTextContent("•••-••-9981");
  });

  it("clears an already-loaded reconciliation panel immediately when the Patient ID changes", async () => {
    vi.mocked(apiFetch).mockResolvedValueOnce(
      jsonResponse(reconciliationFor(1042, "Maria Gonzalez"))
    );

    render(<RecordsPage />);

    fireEvent.click(screen.getByRole("button", { name: /check for related records/i }));
    await waitFor(() => expect(screen.getByText("Maria Gonzalez")).toBeInTheDocument());

    fireEvent.change(screen.getByLabelText(/patient id/i), { target: { value: "2001" } });

    expect(screen.queryByText("Maria Gonzalez")).not.toBeInTheDocument();
  });

  it("clears an already-loaded AI chart view immediately when the Patient ID changes", async () => {
    vi.mocked(apiFetch).mockResolvedValueOnce(
      jsonResponse(aiViewFor(1042, "Patient 1042 has no active concerns."))
    );

    render(<RecordsPage />);

    fireEvent.click(screen.getByRole("button", { name: /generate ai chart view/i }));
    await waitFor(() =>
      expect(screen.getByText("Patient 1042 has no active concerns.")).toBeInTheDocument()
    );

    fireEvent.change(screen.getByLabelText(/patient id/i), { target: { value: "2001" } });

    expect(screen.queryByText("Patient 1042 has no active concerns.")).not.toBeInTheDocument();
  });

  it("drops a slow reconciliation response for the old patient after the id has moved on", async () => {
    let resolveSlowFetch!: (res: Response) => void;
    vi.mocked(apiFetch).mockReturnValueOnce(
      new Promise<Response>((resolve) => {
        resolveSlowFetch = resolve;
      })
    );

    render(<RecordsPage />);

    // Kick off a reconciliation check for patient 1042, but never let its
    // fetch resolve yet — this is the in-flight-when-the-id-changes case.
    fireEvent.click(screen.getByRole("button", { name: /check for related records/i }));

    // Clinician moves on to a different chart before the first request
    // returns.
    fireEvent.change(screen.getByLabelText(/patient id/i), { target: { value: "2001" } });

    // The slow response for patient 1042 finally lands.
    resolveSlowFetch(jsonResponse(reconciliationFor(1042, "Maria Gonzalez")));

    // It must never render under patient 2001's heading.
    await new Promise((r) => setTimeout(r, 0));
    expect(screen.queryByText("Maria Gonzalez")).not.toBeInTheDocument();
  });
});
