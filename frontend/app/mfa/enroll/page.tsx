"use client";

import { Suspense, useEffect, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import {
  clearPendingMfaChallenge,
  getPendingMfaChallenge,
  getToken,
  setSession,
} from "../../lib/session";
import type {
  MfaEnrollConfirmResponse,
  MfaEnrollStartResponse,
} from "../../lib/types";

type Step = "intro" | "setup" | "confirm" | "backup-codes";

// One flow, two entry points:
//   - forced (config/mfa.yaml mode=enforce, first login for an unenrolled
//     in-scope account): reached from /login with a pending challenge_token
//     in sessionStorage and NO session yet — confirmation mints the session.
//   - voluntary (mode=prompt's nudge, or a signed-in user choosing to set
//     MFA up from wherever this page is linked): reached with a real
//     session already in place — "Skip for now" is offered, since prompt
//     mode never blocks anything.
export default function MfaEnrollPage() {
  return (
    <Suspense fallback={null}>
      <MfaEnrollPageInner />
    </Suspense>
  );
}

function MfaEnrollPageInner() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const voluntary = searchParams.get("voluntary") === "1";

  const [challengeToken, setChallengeToken] = useState<string | null>(null);
  const [hasSession, setHasSession] = useState(false);
  const [step, setStep] = useState<Step>("intro");
  const [otpauthUri, setOtpauthUri] = useState("");
  const [manualKey, setManualKey] = useState("");
  const [code, setCode] = useState("");
  const [backupCodes, setBackupCodes] = useState<string[]>([]);
  const [acknowledged, setAcknowledged] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    const pending = getPendingMfaChallenge();
    const token = getToken();
    if (pending) {
      setChallengeToken(pending.challengeToken);
    } else if (token) {
      setHasSession(true);
    } else {
      // Neither a pending challenge nor a session — nothing to enroll with.
      router.replace("/login");
    }
  }, [router]);

  async function post<T>(path: string, body: Record<string, unknown>): Promise<{ ok: boolean; status: number; data: T & { error?: string; detail?: string } }> {
    const headers: Record<string, string> = { "Content-Type": "application/json" };
    const token = getToken();
    if (token) headers["Authorization"] = `Bearer ${token}`;
    const res = await fetch(path, {
      method: "POST",
      headers,
      body: JSON.stringify(challengeToken ? { challenge_token: challengeToken, ...body } : body),
    });
    const data = await res.json();
    return { ok: res.ok, status: res.status, data };
  }

  async function startEnrollment() {
    setError("");
    setBusy(true);
    const { ok, data } = await post<MfaEnrollStartResponse>("/api/mfa/enroll/start", {});
    setBusy(false);
    if (!ok) {
      setError(data.detail || data.error || "Could not start enrollment. Please try again.");
      return;
    }
    setOtpauthUri(data.otpauth_uri);
    setManualKey(data.manual_entry_key);
    setStep("setup");
  }

  async function confirmEnrollment(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    setBusy(true);
    const { ok, status, data } = await post<MfaEnrollConfirmResponse>("/api/mfa/enroll/confirm", { code });
    setBusy(false);
    if (!ok) {
      if (status === 429) {
        setError("Too many attempts. Please wait a few minutes and try again.");
      } else {
        setError("That code didn't match. Check your authenticator app and try again.");
      }
      return;
    }
    setBackupCodes(data.backup_codes);
    if (data.token && data.user) {
      // Forced (challenge-token) enrollment mints its session right here.
      clearPendingMfaChallenge();
      setSession(data.token, data.user);
    }
    setStep("backup-codes");
  }

  function finish() {
    router.replace("/");
  }

  return (
    <div className="rb-login">
      <main className="rb-login__card" id="rb-main">
        <h1 className="rb-login__title">Two-factor authentication</h1>

        {error && (
          <div className="rb-alert rb-alert--err" role="alert">
            {error}
          </div>
        )}

        {step === "intro" && (
          <>
            <p className="rb-login__sub">
              Add a second step to sign-in using an authenticator app (Google Authenticator, Authy, 1Password, or
              similar). This is required for your account before you can continue signing in.
            </p>
            {voluntary && !challengeToken && (
              <p className="rb-login__sub">You can also skip this for now and set it up later.</p>
            )}
            <button className="rb-btn rb-btn--primary rb-btn--block" disabled={busy} onClick={startEnrollment}>
              {busy ? "Starting…" : "Get started"}
            </button>
            {voluntary && !challengeToken && (
              <div className="rb-login__footer">
                <button type="button" className="rb-btn rb-btn--ghost rb-btn--sm" onClick={finish}>
                  Skip for now
                </button>
              </div>
            )}
          </>
        )}

        {step === "setup" && (
          <>
            <p className="rb-login__sub">
              Scan this in your authenticator app, or enter the key manually if it can&apos;t scan a code.
            </p>
            <div className="rb-field">
              <label className="rb-field__label">Manual entry key</label>
              <input className="rb-input" readOnly value={manualKey} onFocus={(e) => e.currentTarget.select()} />
            </div>
            <details style={{ marginBottom: "1rem" }}>
              <summary>Advanced: raw setup URI</summary>
              <p style={{ wordBreak: "break-all", fontSize: "0.8rem" }}>{otpauthUri}</p>
            </details>
            <button
              className="rb-btn rb-btn--primary rb-btn--block"
              onClick={() => {
                setError("");
                setStep("confirm");
              }}
            >
              I&apos;ve added the account
            </button>
          </>
        )}

        {step === "confirm" && (
          <form onSubmit={confirmEnrollment}>
            <p className="rb-login__sub">Enter the 6-digit code your authenticator app is showing now.</p>
            <div className="rb-field">
              <label className="rb-field__label" htmlFor="enroll-code">
                6-digit code
              </label>
              <input
                id="enroll-code"
                className="rb-input"
                inputMode="numeric"
                autoComplete="one-time-code"
                value={code}
                onChange={(e) => setCode(e.target.value)}
                autoFocus
                required
              />
            </div>
            <button className="rb-btn rb-btn--primary rb-btn--block" disabled={busy} type="submit">
              {busy ? "Verifying…" : "Confirm"}
            </button>
          </form>
        )}

        {step === "backup-codes" && (
          <>
            <p className="rb-login__sub">
              Save these ten backup codes somewhere safe. Each one can be used once, in place of your
              authenticator app, if you lose access to it. They are shown only this one time.
            </p>
            <ul
              aria-label="Backup codes"
              style={{
                listStyle: "none",
                padding: "0.75rem",
                margin: "0 0 1rem",
                background: "var(--rb-surface-2, #f4f6f7)",
                borderRadius: "8px",
                fontFamily: "monospace",
                fontSize: "1rem",
                display: "grid",
                gridTemplateColumns: "1fr 1fr",
                gap: "0.4rem",
              }}
            >
              {backupCodes.map((c) => (
                <li key={c}>{c}</li>
              ))}
            </ul>
            <label style={{ display: "flex", gap: "0.5rem", alignItems: "start", marginBottom: "1rem" }}>
              <input
                type="checkbox"
                checked={acknowledged}
                onChange={(e) => setAcknowledged(e.target.checked)}
              />
              <span>I&apos;ve saved these backup codes somewhere safe.</span>
            </label>
            <button className="rb-btn rb-btn--primary rb-btn--block" disabled={!acknowledged} onClick={finish}>
              Done
            </button>
          </>
        )}
      </main>
    </div>
  );
}
