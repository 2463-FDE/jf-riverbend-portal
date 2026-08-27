"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { clearPendingMfaChallenge, getPendingMfaChallenge, setSession } from "../../lib/session";
import type { MfaVerifyResponse } from "../../lib/types";

// The login-CHALLENGE screen — for an account that already completed
// enrollment. A newly-enrolled account never lands here: /mfa/enroll/confirm
// mints its session in the same call that confirms enrollment (see
// services/gateway/app.py::confirm_mfa_enrollment).
export default function LoginMfaPage() {
  const router = useRouter();
  const [challengeToken, setChallengeToken] = useState<string | null>(null);
  const [method, setMethod] = useState<"code" | "backup_code">("code");
  const [value, setValue] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    const pending = getPendingMfaChallenge();
    if (!pending) {
      // No pending challenge (a stale bookmark, a page reload after the
      // token already expired and was cleared) — the only safe next step
      // is a fresh sign-in, never a guess about why.
      router.replace("/login");
      return;
    }
    setChallengeToken(pending.challengeToken);
  }, [router]);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!challengeToken) return;
    setError("");
    setBusy(true);
    try {
      const res = await fetch("/api/mfa/verify", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          challenge_token: challengeToken,
          ...(method === "code" ? { code: value } : { backup_code: value }),
        }),
      });
      const data = (await res.json()) as Partial<MfaVerifyResponse> & { error?: string; detail?: string };
      if (!res.ok || !data.token || !data.user) {
        if (res.status === 401) {
          // Same generic message regardless of WHY — expired-but-unknown vs
          // wrong-code are the same "start over" instruction to the user,
          // and distinguishing them would tell an attacker which is which.
          setError("That code didn't work, or your session expired. Enter a fresh code, or sign in again.");
        } else if (res.status === 429) {
          setError("Too many attempts. Please wait a few minutes and try again.");
        } else {
          setError(data.detail || data.error || "Something went wrong. Please try again.");
        }
        setBusy(false);
        return;
      }
      clearPendingMfaChallenge();
      setSession(data.token, data.user);
      router.replace("/");
    } catch {
      setError("Could not reach the portal. Please try again.");
      setBusy(false);
    }
  }

  return (
    <div className="rb-login">
      <main className="rb-login__card" id="rb-main">
        <h1 className="rb-login__title">Verify it&apos;s you</h1>
        <p className="rb-login__sub">
          Enter the 6-digit code from your authenticator app, or one of your backup codes.
        </p>

        {error && (
          <div className="rb-alert rb-alert--err" role="alert">
            {error}
          </div>
        )}

        <form onSubmit={submit}>
          <div className="rb-field" role="radiogroup" aria-label="Verification method" style={{ display: "flex", gap: "1rem", marginBottom: "0.75rem" }}>
            <label>
              <input
                type="radio"
                name="mfa-method"
                checked={method === "code"}
                onChange={() => {
                  setMethod("code");
                  setValue("");
                }}
              />{" "}
              Authenticator code
            </label>
            <label>
              <input
                type="radio"
                name="mfa-method"
                checked={method === "backup_code"}
                onChange={() => {
                  setMethod("backup_code");
                  setValue("");
                }}
              />{" "}
              Backup code
            </label>
          </div>

          <div className="rb-field">
            <label className="rb-field__label" htmlFor="mfa-value">
              {method === "code" ? "6-digit code" : "Backup code"}
            </label>
            <input
              id="mfa-value"
              className="rb-input"
              inputMode={method === "code" ? "numeric" : "text"}
              autoComplete="one-time-code"
              value={value}
              onChange={(e) => setValue(e.target.value)}
              autoFocus
              required
            />
          </div>

          <button className="rb-btn rb-btn--primary rb-btn--block" disabled={busy || !challengeToken} type="submit">
            {busy ? (
              <>
                <span className="rb-spinner" aria-hidden="true" /> Verifying…
              </>
            ) : (
              "Verify"
            )}
          </button>
        </form>

        <div className="rb-login__hint">
          Lost your device and out of backup codes? Contact your supervisor — MFA can only be reset by an
          administrator, and never by yourself.
        </div>

        <div className="rb-login__footer">
          <button
            type="button"
            className="rb-btn rb-btn--ghost rb-btn--sm"
            onClick={() => {
              clearPendingMfaChallenge();
              router.replace("/login");
            }}
          >
            Start over
          </button>
        </div>
      </main>
    </div>
  );
}
