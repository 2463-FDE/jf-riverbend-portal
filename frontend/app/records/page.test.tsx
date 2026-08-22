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

    // The screen starts with no patient id (2026-08-22) — set one explicitly,
    // since there is no more implicit default. Leaving this out doesn't just
    // fail THIS test: the guarded loader below returns early without ever
    // calling apiFetch, so this test's queued mock response is never consumed
    // and leaks into whichever test runs next (this file has no
    // beforeEach(vi.clearAllMocks()), by design — see the other tests' single
    // exact-shot mocks).
    fireEvent.change(screen.getByLabelText(/patient id/i), { target: { value: "1042" } });
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

    // The screen starts with no patient id (2026-08-22) — set one explicitly,
    // since there is no more implicit default.
    fireEvent.change(screen.getByLabelText(/patient id/i), { target: { value: "1042" } });
    fireEvent.click(screen.getByRole("button", { name: /check for related records/i }));

    // Two rows both start with "Maria Gonzalez" (the canonical chart and the
    // "(possible match)" duplicate), and each TD's full textContent now also
    // includes the id and source label (combined "{Name} — Patient ID {id}"
    // format, 2026-08-22) — this checks AT LEAST one TD carries the name, not
    // that exactly one node equals it exactly.
    const gonzalezCell = (_: string, el: Element | null) =>
      el?.tagName === "TD" && (el.textContent?.includes("Maria Gonzalez") ?? false);
    await waitFor(() => expect(screen.getAllByText(gonzalezCell).length).toBeGreaterThan(0));

    fireEvent.change(screen.getByLabelText(/patient id/i), { target: { value: "2001" } });

    expect(screen.queryAllByText(gonzalezCell).length).toBe(0);
  });

  it("clears an already-loaded AI chart view immediately when the Patient ID changes", async () => {
    vi.mocked(apiFetch).mockResolvedValueOnce(
      jsonResponse(aiViewFor(1042, "Patient 1042 has no active concerns."))
    );

    render(<RecordsPage />);

    // The screen starts with no patient id (2026-08-22) — set one
    // explicitly, since there is no more implicit default.
    fireEvent.change(screen.getByLabelText(/patient id/i), { target: { value: "1042" } });

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

    // The screen starts with no patient id (2026-08-22) — set one
    // explicitly, since there is no more implicit default.
    fireEvent.change(screen.getByLabelText(/patient id/i), { target: { value: "1042" } });

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
    const gonzalezCellAfter = (_: string, el: Element | null) =>
      el?.tagName === "TD" && (el.textContent?.includes("Maria Gonzalez") ?? false);
    expect(screen.queryAllByText(gonzalezCellAfter).length).toBe(0);
  });
});

describe("portal access from the records screen", () => {
  // This test exists because of a gap that ten passing component tests did
  // not catch: PatientInvitation was fully built and covered, but never
  // mounted in any page. It was unreachable in the running app while its own
  // suite was green. Testing a component in isolation says nothing about
  // whether a user can get to it — so this renders the PAGE and looks for it.
  it("offers portal invitation for the patient currently loaded", () => {
    render(<RecordsPage />);

    expect(
      screen.getByRole("button", { name: /issue invitation/i })
    ).toBeInTheDocument();
  });

  it("issues against the patient id shown in the field, not a hardcoded one", async () => {
    // The panel has to follow the patient being looked at. Issuing a chart
    // credential against the wrong patient is the worst failure this screen
    // could have.
    vi.mocked(apiFetch).mockResolvedValue(
      { ok: true, status: 201, json: async () => ({ code: "ABCD-EFGH-JKMN-PQRS" }) } as Response
    );
    render(<RecordsPage />);

    fireEvent.change(screen.getByLabelText(/patient id/i), { target: { value: "1737" } });
    fireEvent.click(screen.getByRole("button", { name: /issue invitation/i }));

    await waitFor(() =>
      expect(vi.mocked(apiFetch)).toHaveBeenCalledWith(
        "/api/patients/1737/invitation",
        expect.objectContaining({ method: "POST" })
      )
    );
  });
});

describe("Patient ID input — no default, no request until asked", () => {
  it("starts with no value and the 'Patient ID' placeholder", () => {
    render(<RecordsPage />);

    const input = screen.getByLabelText(/patient id/i) as HTMLInputElement;
    expect(input.value).toBe("");
    expect(input.placeholder).toBe("Patient ID");
  });

  it("makes no patient-specific request on initial render", () => {
    // This file has no beforeEach(vi.clearAllMocks()) — apiFetch's call count
    // accumulates across every test in the run, by the existing file's own
    // design — so this checks the DELTA across render(), not an absolute
    // "never called", which would be meaningless after the first test.
    const before = vi.mocked(apiFetch).mock.calls.length;
    render(<RecordsPage />);

    expect(vi.mocked(apiFetch).mock.calls.length).toBe(before);
  });

  it("entering 1042 and loading shows Maria Gonzalez's records", async () => {
    vi.mocked(apiFetch).mockImplementation(async (url: string) => {
      if (url.includes("/name")) return jsonResponse({ id: 1042, name: "Maria Gonzalez" });
      return jsonResponse({
        patient_id: 1042,
        encounters: [{
          encounter: { id: 1, type: "office_visit", provider: "Dr. Patel", summary: "Follow-up" },
          records: [{ id: 1, kind: "note", body: "Patient stable." }],
        }],
      });
    });

    render(<RecordsPage />);
    fireEvent.change(screen.getByLabelText(/patient id/i), { target: { value: "1042" } });
    fireEvent.click(screen.getByRole("button", { name: /^load$/i }));

    // The Patient ID field already shows the id — the adjacent name box
    // (2026-08-22: nameOnly on this screen) must not repeat it.
    await waitFor(() => expect(screen.getByText("Maria Gonzalez")).toBeInTheDocument());
    expect(screen.queryByText(/Patient ID 1042/)).not.toBeInTheDocument();
    expect((screen.getByLabelText(/patient id/i) as HTMLInputElement).value).toBe("1042");
    expect(apiFetch).toHaveBeenCalledWith(expect.stringContaining("patient_id=1042"));
  });

  it("clears Maria's information immediately when the id changes to another patient", async () => {
    vi.mocked(apiFetch).mockImplementation(async (url: string) => {
      if (url.includes("/name")) return jsonResponse({ id: 1042, name: "Maria Gonzalez" });
      return jsonResponse({
        patient_id: 1042,
        encounters: [{
          encounter: { id: 1, type: "office_visit", provider: "Dr. Patel", summary: "Follow-up" },
          records: [{ id: 1, kind: "note", body: "Patient stable." }],
        }],
      });
    });

    render(<RecordsPage />);
    fireEvent.change(screen.getByLabelText(/patient id/i), { target: { value: "1042" } });
    fireEvent.click(screen.getByRole("button", { name: /^load$/i }));
    await waitFor(() => expect(screen.getByText("Maria Gonzalez")).toBeInTheDocument());

    fireEvent.change(screen.getByLabelText(/patient id/i), { target: { value: "1737" } });

    expect(screen.queryByText(/Maria Gonzalez/)).not.toBeInTheDocument();
  });

  it("a blank id cannot load records", () => {
    render(<RecordsPage />);
    const before = vi.mocked(apiFetch).mock.calls.length;

    fireEvent.click(screen.getByRole("button", { name: /^load$/i }));

    expect(vi.mocked(apiFetch).mock.calls.length).toBe(before);
  });

  it("a non-numeric id cannot load records", () => {
    render(<RecordsPage />);
    fireEvent.change(screen.getByLabelText(/patient id/i), { target: { value: "abc" } });
    const before = vi.mocked(apiFetch).mock.calls.length;

    fireEvent.click(screen.getByRole("button", { name: /^load$/i }));

    expect(vi.mocked(apiFetch).mock.calls.length).toBe(before);
  });

  it("a blank id cannot issue an invitation", () => {
    render(<RecordsPage />);

    const issueButton = screen.getByRole("button", { name: /issue invitation/i });
    expect(issueButton).toBeDisabled();
    const before = vi.mocked(apiFetch).mock.calls.length;

    fireEvent.click(issueButton);

    expect(vi.mocked(apiFetch).mock.calls.length).toBe(before);
  });
});
