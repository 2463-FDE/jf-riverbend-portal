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
    // 409 is not ONE thing — the backend distinguishes LIVE_INVITATION from
    // ACTIVE_PORTAL_ACCOUNT by a machine-readable reason, never by English
    // text. This is the LIVE_INVITATION case, the one a revoke control
    // actually helps with.
    vi.mocked(apiFetch).mockResolvedValue(err(409, { detail: { reason: "LIVE_INVITATION", message: "this patient already has an unexpired invitation; revoke it before issuing another" } }));

    render(<PatientInvitation patientId="1042" />);
    fireEvent.click(screen.getByRole("button", { name: /issue invitation/i }));

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toMatch(/already has an unexpired invitation/i);
    expect(screen.getByRole("button", { name: /revoke existing invitation/i })).toBeInTheDocument();
  });

  it("explains ACTIVE_PORTAL_ACCOUNT distinctly and offers no revoke control", async () => {
    // The other 409: this patient already has an active portal account (no
    // outstanding invitation for a revoke button to act on at all). Showing
    // "revoke existing invitation" here would be an action with nothing
    // behind it.
    vi.mocked(apiFetch).mockResolvedValue(err(409, { detail: { reason: "ACTIVE_PORTAL_ACCOUNT", message: "this patient already has an active portal account" } }));

    render(<PatientInvitation patientId="1738" />);
    fireEvent.click(screen.getByRole("button", { name: /issue invitation/i }));

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toMatch(/already has an active portal account/i);
    expect(screen.queryByRole("button", { name: /revoke/i })).not.toBeInTheDocument();
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

describe("clearing an invitation that is in the way", () => {
  beforeEach(() => vi.clearAllMocks());

  it("offers a revoke control only after a 409, and not before", async () => {
    // Revoking is a corrective action. Offering it during ordinary
    // registration invites someone to cancel a code a patient is holding.
    vi.mocked(apiFetch).mockResolvedValue(ok({ code: CODE }));
    render(<PatientInvitation patientId="1042" />);

    expect(screen.queryByRole("button", { name: /revoke/i })).not.toBeInTheDocument();
  });

  it("offers revoke when the patient already has an unexpired invitation", async () => {
    vi.mocked(apiFetch).mockResolvedValue(err(409, { detail: { reason: "LIVE_INVITATION", message: "this patient already has an unexpired invitation; revoke it before issuing another" } }));
    render(<PatientInvitation patientId="1042" />);
    fireEvent.click(screen.getByRole("button", { name: /issue invitation/i }));

    expect(await screen.findByRole("button", { name: /revoke existing invitation/i })).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent(/unexpired invitation/i);
  });

  it("offers no revoke control for an active portal account, even after clicking issue", async () => {
    vi.mocked(apiFetch).mockResolvedValue(err(409, { detail: { reason: "ACTIVE_PORTAL_ACCOUNT", message: "this patient already has an active portal account" } }));
    render(<PatientInvitation patientId="1738" />);
    fireEvent.click(screen.getByRole("button", { name: /issue invitation/i }));

    await screen.findByRole("alert");
    expect(screen.queryByRole("button", { name: /revoke/i })).not.toBeInTheDocument();
  });

  it("clears the block after revoking so a new code can be issued", async () => {
    vi.mocked(apiFetch).mockResolvedValueOnce(err(409, { detail: { reason: "LIVE_INVITATION", message: "this patient already has an unexpired invitation; revoke it before issuing another" } }));
    render(<PatientInvitation patientId="1042" />);
    fireEvent.click(screen.getByRole("button", { name: /issue invitation/i }));

    const revokeButton = await screen.findByRole("button", { name: /revoke existing invitation/i });
    vi.mocked(apiFetch).mockResolvedValueOnce({ ok: true, status: 200, json: async () => ({ revoked: 1 }) } as Response);
    fireEvent.click(revokeButton);

    await waitFor(() =>
      expect(screen.getByText(/previous invitation was revoked/i)).toBeInTheDocument()
    );
    // The revoke control goes away once there is nothing to revoke.
    expect(screen.queryByRole("button", { name: /revoke existing/i })).not.toBeInTheDocument();
  });

  it("allows issuing a replacement invitation after a successful revoke", async () => {
    vi.mocked(apiFetch).mockResolvedValueOnce(err(409, { detail: { reason: "LIVE_INVITATION", message: "this patient already has an unexpired invitation; revoke it before issuing another" } }));
    render(<PatientInvitation patientId="1042" />);
    fireEvent.click(screen.getByRole("button", { name: /issue invitation/i }));

    const revokeButton = await screen.findByRole("button", { name: /revoke existing invitation/i });
    vi.mocked(apiFetch).mockResolvedValueOnce({ ok: true, status: 200, json: async () => ({ revoked: 1 }) } as Response);
    fireEvent.click(revokeButton);
    await screen.findByText(/previous invitation was revoked/i);

    vi.mocked(apiFetch).mockResolvedValueOnce(ok({ code: CODE }));
    fireEvent.click(screen.getByRole("button", { name: /issue invitation/i }));

    expect(await screen.findByText(CODE)).toBeInTheDocument();
  });

  it("does not claim success when revoking fails", async () => {
    // A desk that believes it cleared the invitation will keep retrying an
    // issue that cannot succeed.
    vi.mocked(apiFetch).mockResolvedValueOnce(err(409, { detail: { reason: "LIVE_INVITATION", message: "this patient already has an unexpired invitation; revoke it before issuing another" } }));
    render(<PatientInvitation patientId="1042" />);
    fireEvent.click(screen.getByRole("button", { name: /issue invitation/i }));

    const revokeButton = await screen.findByRole("button", { name: /revoke existing invitation/i });
    vi.mocked(apiFetch).mockResolvedValueOnce(err(503));
    fireEvent.click(revokeButton);

    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent(/could not revoke/i));
    expect(screen.queryByText(/previous invitation was revoked/i)).not.toBeInTheDocument();
  });
});
