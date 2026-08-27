import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

const replace = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace }),
}));

const getPendingMfaChallenge = vi.fn();
const clearPendingMfaChallenge = vi.fn();
const setSession = vi.fn();
vi.mock("../../lib/session", () => ({
  getPendingMfaChallenge: () => getPendingMfaChallenge(),
  clearPendingMfaChallenge: () => clearPendingMfaChallenge(),
  setSession: (...args: unknown[]) => setSession(...args),
}));

import LoginMfaPage from "./page";

function jsonResponse(body: unknown, ok = true, status = ok ? 200 : 401): Response {
  return { ok, status, json: async () => body } as Response;
}

describe("login-challenge screen", () => {
  beforeEach(() => {
    replace.mockClear();
    clearPendingMfaChallenge.mockClear();
    setSession.mockClear();
    getPendingMfaChallenge.mockReset();
    (global.fetch as unknown) = vi.fn();
  });

  it("redirects to /login when there is no pending challenge", async () => {
    getPendingMfaChallenge.mockReturnValue(null);

    render(<LoginMfaPage />);

    await waitFor(() => expect(replace).toHaveBeenCalledWith("/login"));
  });

  it("does not redirect when a pending challenge exists", async () => {
    getPendingMfaChallenge.mockReturnValue({ challengeToken: "chal-1", enrollmentRequired: false });

    render(<LoginMfaPage />);

    await screen.findByText(/verify it/i);
    expect(replace).not.toHaveBeenCalled();
  });

  it("submits the authenticator code and, on success, stores the session and goes to the dashboard", async () => {
    getPendingMfaChallenge.mockReturnValue({ challengeToken: "chal-1", enrollmentRequired: false });
    vi.mocked(global.fetch).mockResolvedValue(
      jsonResponse({ token: "tok-1", user: { username: "drnguyen", full_name: "Dr Nguyen", role: "clinician" } })
    );

    render(<LoginMfaPage />);
    await screen.findByText(/verify it/i);

    fireEvent.change(screen.getByLabelText(/6-digit code/i), { target: { value: "123456" } });
    fireEvent.click(screen.getByRole("button", { name: /^verify$/i }));

    await waitFor(() => expect(setSession).toHaveBeenCalledWith("tok-1", expect.objectContaining({ username: "drnguyen" })));
    expect(clearPendingMfaChallenge).toHaveBeenCalled();
    expect(replace).toHaveBeenCalledWith("/");

    const [url, init] = vi.mocked(global.fetch).mock.calls[0];
    expect(url).toBe("/api/mfa/verify");
    expect(JSON.parse((init as RequestInit).body as string)).toEqual({ challenge_token: "chal-1", code: "123456" });
  });

  it("shows a generic error and does not store a session on a wrong code", async () => {
    getPendingMfaChallenge.mockReturnValue({ challengeToken: "chal-1", enrollmentRequired: false });
    vi.mocked(global.fetch).mockResolvedValue(jsonResponse({ detail: "invalid code" }, false, 401));

    render(<LoginMfaPage />);
    await screen.findByText(/verify it/i);

    fireEvent.change(screen.getByLabelText(/6-digit code/i), { target: { value: "000000" } });
    fireEvent.click(screen.getByRole("button", { name: /^verify$/i }));

    await screen.findByRole("alert");
    expect(setSession).not.toHaveBeenCalled();
    expect(replace).not.toHaveBeenCalledWith("/");
  });

  it("shows a rate-limit-specific message on 429", async () => {
    getPendingMfaChallenge.mockReturnValue({ challengeToken: "chal-1", enrollmentRequired: false });
    vi.mocked(global.fetch).mockResolvedValue(jsonResponse({}, false, 429));

    render(<LoginMfaPage />);
    await screen.findByText(/verify it/i);
    fireEvent.change(screen.getByLabelText(/6-digit code/i), { target: { value: "000000" } });
    fireEvent.click(screen.getByRole("button", { name: /^verify$/i }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/too many attempts/i);
  });

  it("switches to the backup-code field and submits backup_code instead of code", async () => {
    getPendingMfaChallenge.mockReturnValue({ challengeToken: "chal-1", enrollmentRequired: false });
    vi.mocked(global.fetch).mockResolvedValue(
      jsonResponse({ token: "tok-1", user: { username: "drnguyen", full_name: "Dr Nguyen", role: "clinician" } })
    );

    render(<LoginMfaPage />);
    await screen.findByText(/verify it/i);

    fireEvent.click(screen.getByRole("radio", { name: /backup code/i }));
    fireEvent.change(document.getElementById("mfa-value") as HTMLInputElement, {
      target: { value: "ABCDE12345" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^verify$/i }));

    await waitFor(() => expect(global.fetch).toHaveBeenCalled());
    const [, init] = vi.mocked(global.fetch).mock.calls[0];
    expect(JSON.parse((init as RequestInit).body as string)).toEqual({
      challenge_token: "chal-1",
      backup_code: "ABCDE12345",
    });
  });
});
