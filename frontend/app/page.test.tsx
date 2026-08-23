import { describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";

// The single most important property of this route: a patient session must
// never render the staff dashboard below it, which links straight to staff
// actions (Intake, Release of Information) a patient account is refused
// outright, and whose appointments/results cards are hardcoded empty for
// everyone regardless of who is actually signed in.

vi.mock("./lib/session", () => ({
  apiFetch: vi.fn(),
  getUser: vi.fn(),
}));

import DashboardPage from "./page";
import { apiFetch, getUser } from "./lib/session";

function ok(body: unknown): Response {
  return { ok: true, status: 200, json: async () => body } as Response;
}

describe("/ — role-based landing page", () => {
  it("renders the patient home, not the staff dashboard, for a patient session", async () => {
    vi.mocked(getUser).mockReturnValue({ username: "patient-1737", full_name: "Priya Khan", role: "patient" });
    vi.mocked(apiFetch).mockResolvedValue(ok({ available: false, status: "none", items: [] }));

    render(<DashboardPage />);

    await waitFor(() => expect(screen.getByText(/good day, priya/i)).toBeInTheDocument());
    expect(screen.queryByText(/quick actions/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/start intake/i)).not.toBeInTheDocument();
  });

  it("renders the existing staff dashboard for a staff session", async () => {
    vi.mocked(getUser).mockReturnValue({ username: "frontdesk", full_name: "Front Desk", role: "front_desk" });

    render(<DashboardPage />);

    await waitFor(() => expect(screen.getByText(/good day, front/i)).toBeInTheDocument());
    expect(screen.getByText(/quick actions/i)).toBeInTheDocument();
    expect(screen.getByText(/start intake/i)).toBeInTheDocument();
  });
});
