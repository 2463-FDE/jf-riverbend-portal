import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

// The staff dashboard this replaces asked for nothing and showed nothing
// real. What matters here is the inverse: identity comes from the session
// alone (no id this component could be tricked into substituting), and the
// summary card shows exactly one real status, never a leaked internal detail
// (job id, trace id, model id, validation code) about a version the patient
// may not read.

vi.mock("../lib/session", () => ({
  apiFetch: vi.fn(),
  getUser: () => ({ username: "patient-1737", full_name: "Priya Khan", role: "patient" }),
}));

import PatientHome from "./PatientHome";
import { apiFetch } from "../lib/session";

function ok(body: unknown): Response {
  return { ok: true, status: 200, json: async () => body } as Response;
}
function denied(status: number): Response {
  return { ok: false, status, json: async () => ({}) } as Response;
}

const IDENTITY = { patient_id: 1737, name: "Priya Khan" };

function mockRoutes({
  summary,
  results = { items: [] },
  identity = IDENTITY,
}: {
  summary: unknown;
  results?: unknown;
  identity?: unknown;
}) {
  vi.mocked(apiFetch).mockImplementation(async (url: string) => {
    if (url.includes("/patient/identity")) return ok(identity);
    if (url.includes("/agent-summary/request")) return ok({ version: 2, status: "validated" });
    if (url.includes("/agent-summary")) return ok(summary);
    if (url.includes("/patient/summary")) return ok(results);
    return ok({});
  });
}

describe("PatientHome", () => {
  beforeEach(() => vi.clearAllMocks());

  it("greets the patient and shows their own id, resolved from the session, not a query id", async () => {
    mockRoutes({ summary: { available: false, status: "none" } });
    render(<PatientHome />);

    expect(await screen.findByText(/good day, priya/i)).toBeInTheDocument();
    expect(await screen.findByText(/priya khan — patient id 1737/i)).toBeInTheDocument();

    // No call this component makes ever names a patient id in its URL — the
    // gateway derives it from the bearer token alone.
    for (const [url] of vi.mocked(apiFetch).mock.calls) {
      expect(String(url)).not.toMatch(/\/patients\/\d+/);
    }
  });

  it("shows the approved state and never exposes the approved text itself", async () => {
    mockRoutes({
      summary: { available: true, status: "approved", version: 3, provenance_label: "real" },
    });
    render(<PatientHome />);

    expect(await screen.findByText("Approved")).toBeInTheDocument();
    expect(screen.getByText(/care team has approved a new summary/i)).toBeInTheDocument();
    // This card only ever shows status — the full text lives on /my-results.
    expect(screen.queryByText(/version 3/i)).not.toBeInTheDocument();
  });

  it("shows the pending state and disables requesting again", async () => {
    mockRoutes({ summary: { available: false, status: "pending" } });
    render(<PatientHome />);

    expect(await screen.findByText("Waiting for review")).toBeInTheDocument();
    expect(screen.getByText(/waiting for a clinician to review it/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /request an updated summary/i })).toBeDisabled();
  });

  it("shows a quiet empty state when nothing has ever been requested", async () => {
    mockRoutes({ summary: { available: false, status: "none" }, results: { items: [] } });
    render(<PatientHome />);

    expect(await screen.findByText("Not requested")).toBeInTheDocument();
    expect(screen.getByText(/nothing new to review right now/i)).toBeInTheDocument();
  });

  it("falls back to a results-based state when there are results but no summary", async () => {
    mockRoutes({
      summary: { available: false, status: "none" },
      results: { items: [{ record_id: 1 }, { record_id: 2 }] },
    });
    render(<PatientHome />);

    expect(await screen.findByText(/you have results on file/i)).toBeInTheDocument();
    expect(await screen.findByText(/2 results on file/i)).toBeInTheDocument();
  });

  it("shows a plain-language error without exposing internal detail when the status call fails", async () => {
    vi.mocked(apiFetch).mockImplementation(async (url: string) => {
      if (url.includes("/patient/identity")) return ok(IDENTITY);
      if (url.includes("/agent-summary")) return denied(500);
      if (url.includes("/patient/summary")) return ok({ items: [] });
      return ok({});
    });
    render(<PatientHome />);

    expect(await screen.findByText(/could not load your status/i)).toBeInTheDocument();
    expect(screen.queryByText(/trace|correlation|model_id|validation_code/i)).not.toBeInTheDocument();
  });

  it("requesting an updated summary posts once and shows a plain confirmation", async () => {
    mockRoutes({ summary: { available: false, status: "none" } });
    render(<PatientHome />);

    const button = await screen.findByRole("button", { name: /request an updated summary/i });
    fireEvent.click(button);

    await waitFor(() =>
      expect(
        vi.mocked(apiFetch).mock.calls.some(([url]) => String(url).includes("/agent-summary/request"))
      ).toBe(true)
    );
    expect(await screen.findByText(/your care team will review it/i)).toBeInTheDocument();
  });
});
