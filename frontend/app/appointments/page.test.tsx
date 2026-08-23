import { describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import AppointmentsPage from "./page";

// The Patient ID is already visible in its own input on this screen — the
// adjacent name box (PatientName's `nameOnly` option, 2026-08-22) must show
// only the name, never repeat "Patient ID {id}" a second time.

vi.mock("../lib/session", () => ({ apiFetch: vi.fn() }));

import { apiFetch } from "../lib/session";

function jsonResponse(body: unknown, ok = true): Response {
  return { ok, json: async () => body } as Response;
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
  it("shows the id in its own input and only the name beside it", async () => {
    mockRoutes({ "1042": "Maria Gonzalez" });
    render(<AppointmentsPage />);

    // The default id auto-loads on this screen (unlike records) — wait for
    // the name lookup it triggers to resolve.
    await waitFor(() => expect(screen.getByText("Maria Gonzalez")).toBeInTheDocument());

    expect((screen.getByLabelText(/patient id/i) as HTMLInputElement).value).toBe("1042");
    expect(screen.queryByText(/Patient ID 1042/)).not.toBeInTheDocument();
  });

  it("shows only the name after loading a different patient", async () => {
    mockRoutes({ "1042": "Maria Gonzalez", "1738": "Thomas Johnson" });
    render(<AppointmentsPage />);
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
    await waitFor(() => expect(screen.getByText("Maria Gonzalez")).toBeInTheDocument());

    fireEvent.change(screen.getByLabelText(/patient id/i), { target: { value: "1739" } });
    fireEvent.click(screen.getByRole("button", { name: /^load$/i }));

    await waitFor(() => expect(screen.getByText(/name unavailable/i)).toBeInTheDocument());
    expect(screen.queryByText(/Maria Gonzalez/)).not.toBeInTheDocument();
  });
});
