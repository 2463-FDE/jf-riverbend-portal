import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

// The code is shown ONCE and never retrievable — the gateway stores only a
// hash. Everything asserted here follows from that: the warning has to be
// present, the code has to be on screen, and a failure must not look like
// success (a front desk that thinks it issued a code, but didn't, sends the
// patient away with nothing).

vi.mock("../lib/session", () => ({ apiFetch: vi.fn() }));

import PatientInvitation from "./PatientInvitation";
import { apiFetch } from "../lib/session";

const CODE = "ABCD-EFGH-JKMN-PQRS";

function ok(body: unknown) {
  return { ok: true, status: 201, json: async () => body } as Response;
}
function err(status: number, body: unknown = {}) {
  return { ok: false, status, json: async () => body } as Response;
}

describe("issuing a patient portal invitation", () => {
  beforeEach(() => vi.clearAllMocks());

  it("shows the code after issuing", async () => {
    vi.mocked(apiFetch).mockResolvedValue(ok({ code: CODE, expires_at: "2026-08-28T00:00:00Z" }));

    render(<PatientInvitation patientId="1042" />);
    fireEvent.click(screen.getByRole("button", { name: /issue invitation/i }));

    expect(await screen.findByText(CODE)).toBeInTheDocument();
  });

  it("warns that the code cannot be recovered", async () => {
    // Without this, a front desk closes the panel and the patient is locked out
    // with no way to get the code back.
    vi.mocked(apiFetch).mockResolvedValue(ok({ code: CODE }));

    render(<PatientInvitation patientId="1042" />);
    fireEvent.click(screen.getByRole("button", { name: /issue invitation/i }));
    await screen.findByText(CODE);

    expect(screen.getByText(/shown only once/i)).toBeInTheDocument();
    expect(screen.getByText(/cannot be looked up again/i)).toBeInTheDocument();
  });

  it("explains what to do when the patient already has a live invitation", async () => {
    // 409 is the common case at a busy desk. A status code helps nobody.
    vi.mocked(apiFetch).mockResolvedValue(err(409));

    render(<PatientInvitation patientId="1042" />);
    fireEvent.click(screen.getByRole("button", { name: /issue invitation/i }));

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toMatch(/already has an active invitation/i);
    expect(alert.textContent).toMatch(/revoke/i);
  });

  it("does not show a code when issuing failed", async () => {
    vi.mocked(apiFetch).mockResolvedValue(err(500, { detail: "boom" }));

    render(<PatientInvitation patientId="1042" />);
    fireEvent.click(screen.getByRole("button", { name: /issue invitation/i }));

    await screen.findByRole("alert");
    expect(screen.queryByText(CODE)).not.toBeInTheDocument();
    // Still offers a retry rather than stranding the desk.
    expect(screen.getByRole("button", { name: /issue invitation/i })).toBeEnabled();
  });

  it("surfaces a network failure instead of appearing to succeed", async () => {
    vi.mocked(apiFetch).mockRejectedValue(new Error("network down"));

    render(<PatientInvitation patientId="1042" />);
    fireEvent.click(screen.getByRole("button", { name: /issue invitation/i }));

    expect((await screen.findByRole("alert")).textContent).toMatch(/could not reach/i);
    expect(screen.queryByText(CODE)).not.toBeInTheDocument();
  });

  it("disables the button while the request is in flight", async () => {
    // Double-clicking would burn a second invitation and hit the one-live-
    // invitation constraint, confusing the desk.
    let resolve!: (r: Response) => void;
    vi.mocked(apiFetch).mockReturnValue(new Promise<Response>((r) => (resolve = r)));

    render(<PatientInvitation patientId="1042" />);
    fireEvent.click(screen.getByRole("button", { name: /issue invitation/i }));

    await waitFor(() => expect(screen.getByRole("button", { name: /issuing/i })).toBeDisabled());
    resolve(ok({ code: CODE }));
    await screen.findByText(CODE);
  });
});
