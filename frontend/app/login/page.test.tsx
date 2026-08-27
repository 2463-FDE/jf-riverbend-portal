import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

// Round-1 review (B01): a browser must never end up holding a full session
// AND a pending MFA challenge at the same time — mfa/enroll/page.tsx's
// post() attaches Authorization whenever getToken() returns a value, so a
// stale session left over from a previous (or abandoned) sign-in would
// otherwise reach the gateway alongside a fresh challenge_token for a
// DIFFERENT login.

const replace = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace }),
}));

const clearSession = vi.fn();
const clearPendingMfaChallenge = vi.fn();
const setSession = vi.fn();
const setPendingMfaChallenge = vi.fn();
vi.mock("../lib/session", () => ({
  clearSession: (...args: unknown[]) => clearSession(...args),
  clearPendingMfaChallenge: (...args: unknown[]) => clearPendingMfaChallenge(...args),
  setSession: (...args: unknown[]) => setSession(...args),
  setPendingMfaChallenge: (...args: unknown[]) => setPendingMfaChallenge(...args),
}));

import LoginPage from "./page";

function jsonResponse(body: unknown, ok = true, status = ok ? 200 : 401): Response {
  return { ok, status, json: async () => body } as Response;
}

async function submitLogin() {
  render(<LoginPage />);
  fireEvent.change(screen.getByLabelText(/username/i), { target: { value: "drnguyen" } });
  fireEvent.change(screen.getByLabelText(/password/i), { target: { value: "portal123" } });
  fireEvent.click(screen.getByRole("button", { name: /sign in/i }));
}

describe("login page — session/challenge exclusivity (B01)", () => {
  beforeEach(() => {
    replace.mockClear();
    clearSession.mockClear();
    clearPendingMfaChallenge.mockClear();
    setSession.mockClear();
    setPendingMfaChallenge.mockClear();
    (global.fetch as unknown) = vi.fn();
  });

  it("clears the previous session BEFORE storing a newly returned MFA challenge", async () => {
    vi.mocked(global.fetch).mockResolvedValue(
      jsonResponse({ mfa: { required: true, enrollment_required: true, challenge_token: "chal-b" } })
    );

    await submitLogin();

    await waitFor(() => expect(setPendingMfaChallenge).toHaveBeenCalledWith("chal-b", true));
    expect(clearSession).toHaveBeenCalled();
    expect(clearPendingMfaChallenge).toHaveBeenCalled();

    // Ordering matters: the stale session is gone before the new challenge
    // is stored, so there is never a moment both are present together.
    const clearOrder = clearSession.mock.invocationCallOrder[0];
    const setOrder = setPendingMfaChallenge.mock.invocationCallOrder[0];
    expect(clearOrder).toBeLessThan(setOrder);

    expect(replace).toHaveBeenCalledWith("/mfa/enroll");
  });

  it("routes to the login-challenge screen (not enrollment) when already enrolled", async () => {
    vi.mocked(global.fetch).mockResolvedValue(
      jsonResponse({ mfa: { required: true, enrollment_required: false, challenge_token: "chal-c" } })
    );

    await submitLogin();

    await waitFor(() => expect(replace).toHaveBeenCalledWith("/login/mfa"));
    expect(clearSession).toHaveBeenCalled();
  });

  it("clears a stale pending MFA challenge before storing a normal full session", async () => {
    vi.mocked(global.fetch).mockResolvedValue(
      jsonResponse({
        token: "tok-1",
        mfa: { required: false, prompt: false, enrolled: false },
        user: { username: "drnguyen", full_name: "Dr Nguyen", role: "clinician" },
      })
    );

    await submitLogin();

    await waitFor(() => expect(setSession).toHaveBeenCalledWith("tok-1", expect.any(Object)));
    expect(clearPendingMfaChallenge).toHaveBeenCalled();
    // A full session issuing is not itself a "stale session" case — clearSession
    // is specific to the challenge-issued branch, not this one.
    expect(clearSession).not.toHaveBeenCalled();

    const clearOrder = clearPendingMfaChallenge.mock.invocationCallOrder[0];
    const setOrder = setSession.mock.invocationCallOrder[0];
    expect(clearOrder).toBeLessThan(setOrder);

    expect(replace).toHaveBeenCalledWith("/");
  });

  it("clears a stale pending challenge on the prompt-mode nudge path too", async () => {
    vi.mocked(global.fetch).mockResolvedValue(
      jsonResponse({
        token: "tok-2",
        mfa: { required: false, prompt: true, enrolled: false },
        user: { username: "drnguyen", full_name: "Dr Nguyen", role: "clinician" },
      })
    );

    await submitLogin();

    await waitFor(() => expect(replace).toHaveBeenCalledWith("/mfa/enroll?voluntary=1"));
    expect(clearPendingMfaChallenge).toHaveBeenCalled();
  });

  it("does not touch session/challenge storage on a failed login", async () => {
    vi.mocked(global.fetch).mockResolvedValue(jsonResponse({ error: "invalid username or password" }, false, 401));

    await submitLogin();

    await screen.findByRole("alert");
    expect(setSession).not.toHaveBeenCalled();
    expect(setPendingMfaChallenge).not.toHaveBeenCalled();
    expect(clearSession).not.toHaveBeenCalled();
  });
});
