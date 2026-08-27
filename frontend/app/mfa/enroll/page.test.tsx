import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

const replace = vi.fn();
let searchParamsValue = "";
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace }),
  useSearchParams: () => new URLSearchParams(searchParamsValue),
}));

const getPendingMfaChallenge = vi.fn();
const clearPendingMfaChallenge = vi.fn();
const getToken = vi.fn();
const setSession = vi.fn();
vi.mock("../../lib/session", () => ({
  getPendingMfaChallenge: () => getPendingMfaChallenge(),
  clearPendingMfaChallenge: () => clearPendingMfaChallenge(),
  getToken: () => getToken(),
  setSession: (...args: unknown[]) => setSession(...args),
}));

import MfaEnrollPage from "./page";

function jsonResponse(body: unknown, ok = true, status = ok ? 200 : 401): Response {
  return { ok, status, json: async () => body } as Response;
}

describe("MFA enrollment screen", () => {
  beforeEach(() => {
    replace.mockClear();
    clearPendingMfaChallenge.mockClear();
    setSession.mockClear();
    getPendingMfaChallenge.mockReset();
    getToken.mockReset();
    searchParamsValue = "";
    (global.fetch as unknown) = vi.fn();
  });

  it("redirects to /login when there is neither a pending challenge nor a session", async () => {
    getPendingMfaChallenge.mockReturnValue(null);
    getToken.mockReturnValue(null);

    render(<MfaEnrollPage />);

    await waitFor(() => expect(replace).toHaveBeenCalledWith("/login"));
  });

  it("forced flow: start -> confirm -> backup codes -> session is stored from the confirm response", async () => {
    getPendingMfaChallenge.mockReturnValue({ challengeToken: "chal-1", enrollmentRequired: true });
    getToken.mockReturnValue(null);
    const fetchMock = vi.fn();
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ otpauth_uri: "otpauth://totp/x", manual_entry_key: "JBSWY3DPEHPK3PXP" })
    );
    fetchMock.mockResolvedValueOnce(
      jsonResponse({
        status: "enrolled",
        backup_codes: Array.from({ length: 10 }, (_, i) => `CODE${i}`),
        token: "tok-new",
        user: { username: "drnguyen", full_name: "Dr Nguyen", role: "clinician" },
      })
    );
    global.fetch = fetchMock;

    render(<MfaEnrollPage />);
    await screen.findByText(/two-factor authentication/i);

    fireEvent.click(screen.getByRole("button", { name: /get started/i }));
    await screen.findByText(/manual entry key/i);
    expect(screen.getByDisplayValue("JBSWY3DPEHPK3PXP")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /i've added the account/i }));
    fireEvent.change(screen.getByLabelText(/6-digit code/i), { target: { value: "123456" } });
    fireEvent.click(screen.getByRole("button", { name: /^confirm$/i }));

    await screen.findByText(/save these ten backup codes/i);
    expect(screen.getByText("CODE0")).toBeInTheDocument();
    expect(setSession).toHaveBeenCalledWith("tok-new", expect.objectContaining({ username: "drnguyen" }));
    expect(clearPendingMfaChallenge).toHaveBeenCalled();

    // "Done" is disabled until the acknowledgement checkbox is checked.
    const doneBtn = screen.getByRole("button", { name: /^done$/i });
    expect(doneBtn).toBeDisabled();
    fireEvent.click(screen.getByRole("checkbox"));
    expect(doneBtn).not.toBeDisabled();
    fireEvent.click(doneBtn);
    expect(replace).toHaveBeenCalledWith("/");

    // enroll/start was called WITH the challenge_token, not an Authorization flow.
    const startBody = JSON.parse((fetchMock.mock.calls[0][1] as RequestInit).body as string);
    expect(startBody).toEqual({ challenge_token: "chal-1" });
  });

  it("voluntary flow (prompt nudge): offers Skip for now and does not mint a new session", async () => {
    searchParamsValue = "voluntary=1";
    getPendingMfaChallenge.mockReturnValue(null);
    getToken.mockReturnValue("existing-session-token");

    render(<MfaEnrollPage />);
    await screen.findByText(/two-factor authentication/i);

    expect(screen.getByRole("button", { name: /skip for now/i })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /skip for now/i }));

    expect(replace).toHaveBeenCalledWith("/");
    expect(setSession).not.toHaveBeenCalled();
  });

  it("voluntary flow: confirming enrollment does not receive/store a new session token", async () => {
    getPendingMfaChallenge.mockReturnValue(null);
    getToken.mockReturnValue("existing-session-token");
    const fetchMock = vi.fn();
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ otpauth_uri: "otpauth://totp/x", manual_entry_key: "SECRETKEY" })
    );
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ status: "enrolled", backup_codes: Array.from({ length: 10 }, (_, i) => `C${i}`) })
    );
    global.fetch = fetchMock;

    render(<MfaEnrollPage />);
    await screen.findByText(/two-factor authentication/i);
    fireEvent.click(screen.getByRole("button", { name: /get started/i }));
    await screen.findByText(/manual entry key/i);
    fireEvent.click(screen.getByRole("button", { name: /i've added the account/i }));
    fireEvent.change(screen.getByLabelText(/6-digit code/i), { target: { value: "654321" } });
    fireEvent.click(screen.getByRole("button", { name: /^confirm$/i }));

    await screen.findByText(/save these ten backup codes/i);
    expect(setSession).not.toHaveBeenCalled();

    // Authorization header was attached instead of a challenge_token.
    const [, startInit] = fetchMock.mock.calls[0];
    expect((startInit as RequestInit).headers).toMatchObject({ Authorization: "Bearer existing-session-token" });
  });

  it("shows an error and stays on the confirm step for a wrong code", async () => {
    getPendingMfaChallenge.mockReturnValue({ challengeToken: "chal-1", enrollmentRequired: true });
    getToken.mockReturnValue(null);
    const fetchMock = vi.fn();
    fetchMock.mockResolvedValueOnce(jsonResponse({ otpauth_uri: "otpauth://totp/x", manual_entry_key: "K" }));
    fetchMock.mockResolvedValueOnce(jsonResponse({ detail: "invalid code" }, false, 401));
    global.fetch = fetchMock;

    render(<MfaEnrollPage />);
    await screen.findByText(/two-factor authentication/i);
    fireEvent.click(screen.getByRole("button", { name: /get started/i }));
    await screen.findByText(/manual entry key/i);
    fireEvent.click(screen.getByRole("button", { name: /i've added the account/i }));
    fireEvent.change(screen.getByLabelText(/6-digit code/i), { target: { value: "000000" } });
    fireEvent.click(screen.getByRole("button", { name: /^confirm$/i }));

    await screen.findByRole("alert");
    expect(screen.getByLabelText(/6-digit code/i)).toBeInTheDocument(); // still on the confirm step
    expect(setSession).not.toHaveBeenCalled();
  });
});
