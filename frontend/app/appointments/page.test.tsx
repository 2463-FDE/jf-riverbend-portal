import { describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import AppointmentsPage from "./page";

// The Patient ID is already visible in its own input on this screen — the
// adjacent name box (PatientName's `nameOnly` option, 2026-08-22) must show
// only the name, never repeat "Patient ID {id}" a second time.

vi.mock("../lib/session", () => ({ apiFetch: vi.fn() }));

import { apiFetch } from "../lib/session";

function jsonResponse(body: unknown, ok = true, status = ok ? 200 : 500): Response {
  return { ok, status, json: async () => body } as Response;
}

function mockRoutes(nameByPatient: Record<string, string>) {
  vi.mocked(apiFetch).mockImplementation(async (url: string) => {
    if (url.includes("/name")) {
      const id = url.match(/patients\/(\d+)\/name/)?.[1] ?? "";
      return nameByPatient[id]
        ? jsonResponse({ id: Number(id), name: nameByPatient[id] })
        : jsonResponse({ error: "name unavailable" }, false);
    }
    if (url.includes("/appointments")) return jsonResponse({ items: [] });
    if (url.includes("/slots")) return jsonResponse({ items: [] });
    return jsonResponse({});
  });
}

describe("appointments — name-only identity box", () => {
  it("starts with an empty Patient ID field, issues no patient-specific request until Load, then resolves the name", async () => {
    mockRoutes({ "1042": "Maria Gonzalez" });
    render(<AppointmentsPage />);

    const input = screen.getByLabelText(/patient id/i) as HTMLInputElement;
    expect(input.value).toBe("");
    expect(input.placeholder).toBe("Patient ID");

    // /slots is patient-independent and does auto-load; /appointments and
    // /name (both patient-specific) must not fire until Load is pressed.
    await waitFor(() =>
      expect(vi.mocked(apiFetch).mock.calls.some(([url]) => String(url).includes("/slots"))).toBe(true)
    );
    expect(vi.mocked(apiFetch).mock.calls.some(([url]) => String(url).includes("/appointments"))).toBe(false);
    expect(vi.mocked(apiFetch).mock.calls.some(([url]) => String(url).includes("/name"))).toBe(false);
    expect(screen.getByText(/enter a patient id above and press load/i)).toBeInTheDocument();

    fireEvent.change(input, { target: { value: "1042" } });
    fireEvent.click(screen.getByRole("button", { name: /^load$/i }));

    await waitFor(() => expect(screen.getByText("Maria Gonzalez")).toBeInTheDocument());
    expect(input.value).toBe("1042");
    expect(screen.queryByText(/Patient ID 1042/)).not.toBeInTheDocument();
  });

  it("shows only the name after loading a different patient", async () => {
    mockRoutes({ "1042": "Maria Gonzalez", "1738": "Thomas Johnson" });
    render(<AppointmentsPage />);

    fireEvent.change(screen.getByLabelText(/patient id/i), { target: { value: "1042" } });
    fireEvent.click(screen.getByRole("button", { name: /^load$/i }));
    await waitFor(() => expect(screen.getByText("Maria Gonzalez")).toBeInTheDocument());

    fireEvent.change(screen.getByLabelText(/patient id/i), { target: { value: "1738" } });
    fireEvent.click(screen.getByRole("button", { name: /^load$/i }));

    await waitFor(() => expect(screen.getByText("Thomas Johnson")).toBeInTheDocument());
    expect(screen.queryByText(/Maria Gonzalez/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Patient ID 1738/)).not.toBeInTheDocument();
  });

  it("shows 'Name unavailable' rather than a stale or partial identity on denial", async () => {
    mockRoutes({ "1042": "Maria Gonzalez" }); // 1739 deliberately absent -> denied
    render(<AppointmentsPage />);

    fireEvent.change(screen.getByLabelText(/patient id/i), { target: { value: "1042" } });
    fireEvent.click(screen.getByRole("button", { name: /^load$/i }));
    await waitFor(() => expect(screen.getByText("Maria Gonzalez")).toBeInTheDocument());

    fireEvent.change(screen.getByLabelText(/patient id/i), { target: { value: "1739" } });
    fireEvent.click(screen.getByRole("button", { name: /^load$/i }));

    await waitFor(() => expect(screen.getByText(/name unavailable/i)).toBeInTheDocument());
    expect(screen.queryByText(/Maria Gonzalez/)).not.toBeInTheDocument();
  });
});

// w9-fixes P1 4.1 — editing the Patient ID field must drop every trace of
// the previous patient's transient state (error/success banner, typed
// reason, busy spinners) immediately, not just their name/list.
describe("appointments — patient-context reset (w9-fixes 4.1)", () => {
  const SLOT = {
    id: 5,
    provider: "Dr. X",
    location: "Riverbend Main",
    start_at: "2026-09-01T10:00:00Z",
    end_at: "2026-09-01T10:30:00Z",
    status: "open",
  };

  function mockBookingFailure() {
    vi.mocked(apiFetch).mockImplementation(async (url: string, init?: RequestInit) => {
      if (url.includes("/name")) {
        const id = url.match(/patients\/(\d+)\/name/)?.[1] ?? "";
        const names: Record<string, string> = { "1042": "Maria Gonzalez", "1738": "Thomas Johnson" };
        return names[id]
          ? jsonResponse({ id: Number(id), name: names[id] })
          : jsonResponse({ error: "name unavailable" }, false);
      }
      if (init?.method === "POST" && url.includes("/appointments")) {
        return jsonResponse({ error: "slot_taken" }, false);
      }
      if (url.includes("/appointments")) return jsonResponse({ items: [] });
      if (url.includes("/slots")) return jsonResponse({ items: [SLOT] });
      return jsonResponse({});
    });
  }

  it("clears a prior error, typed reason, and busy state before the next patient loads", async () => {
    mockBookingFailure();
    render(<AppointmentsPage />);

    fireEvent.change(screen.getByLabelText(/patient id/i), { target: { value: "1042" } });
    fireEvent.click(screen.getByRole("button", { name: /^load$/i }));
    await waitFor(() => expect(screen.getByText("Maria Gonzalez")).toBeInTheDocument());

    fireEvent.change(screen.getByLabelText(/reason for visit/i), { target: { value: "vaccination" } });
    fireEvent.click(await screen.findByRole("button", { name: /^book$/i }));
    await waitFor(() => expect(screen.getByText(/could not book that slot/i)).toBeInTheDocument());

    // Editing the id — before pressing Load — must drop the stale banner
    // and reason immediately.
    fireEvent.change(screen.getByLabelText(/patient id/i), { target: { value: "1738" } });
    expect(screen.queryByText(/could not book that slot/i)).not.toBeInTheDocument();
    expect((screen.getByLabelText(/reason for visit/i) as HTMLInputElement).value).toBe("");
    expect(screen.queryByText("Maria Gonzalez")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /^load$/i }));
    await waitFor(() => expect(screen.getByText("Thomas Johnson")).toBeInTheDocument());
    expect(screen.queryByText(/could not book that slot/i)).not.toBeInTheDocument();
  });

  it("disables Book until a patient is loaded", async () => {
    mockBookingFailure();
    render(<AppointmentsPage />);

    const book = await screen.findByRole("button", { name: /^book$/i });
    expect(book).toBeDisabled();

    fireEvent.change(screen.getByLabelText(/patient id/i), { target: { value: "1042" } });
    fireEvent.click(screen.getByRole("button", { name: /^load$/i }));
    await waitFor(() => expect(screen.getByText("Maria Gonzalez")).toBeInTheDocument());
    expect(screen.getByRole("button", { name: /^book$/i })).not.toBeDisabled();
  });
});

// Round-1 review (M2): a denied or failed appointments load has no `items`,
// so it used to render exactly like "loaded successfully, zero
// appointments" — indistinguishable from a patient who genuinely has none —
// while still leaving Book enabled for a patient never actually confirmed.
describe("appointments — a failed load surfaces an error, not an empty success (Round-1 review M2)", () => {
  const SLOT = {
    id: 9,
    provider: "Dr. X",
    location: "Riverbend Main",
    start_at: "2026-09-01T10:00:00Z",
    end_at: "2026-09-01T10:30:00Z",
    status: "open",
  };

  function mockLoadStatus(status: number) {
    vi.mocked(apiFetch).mockImplementation(async (url: string) => {
      if (url.includes("/name")) return jsonResponse({ id: 1042, name: "Maria Gonzalez" });
      if (url.includes("/appointments")) return jsonResponse({ detail: "denied" }, false, status);
      if (url.includes("/slots")) return jsonResponse({ items: [SLOT] });
      return jsonResponse({});
    });
  }

  it("shows an authorization error, not 'no appointments', on a 403", async () => {
    mockLoadStatus(403);
    render(<AppointmentsPage />);

    fireEvent.change(screen.getByLabelText(/patient id/i), { target: { value: "1042" } });
    fireEvent.click(screen.getByRole("button", { name: /^load$/i }));

    await waitFor(() => expect(screen.getByText(/not authorized to view/i)).toBeInTheDocument());
    expect(screen.queryByText(/no appointments for this patient/i)).not.toBeInTheDocument();
    expect(screen.queryByText("Maria Gonzalez")).not.toBeInTheDocument(); // loadedPatientId never set
  });

  it("shows a load error, not 'no appointments', on a 500, and keeps Book disabled", async () => {
    mockLoadStatus(500);
    render(<AppointmentsPage />);

    fireEvent.change(screen.getByLabelText(/patient id/i), { target: { value: "1042" } });
    fireEvent.click(screen.getByRole("button", { name: /^load$/i }));

    await waitFor(() => expect(screen.getByText(/could not load appointments/i)).toBeInTheDocument());
    expect(screen.queryByText(/no appointments for this patient/i)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^book$/i })).toBeDisabled();
  });
});
