"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import { clearPendingMfaChallenge, clearSession, setPendingMfaChallenge, setSession } from "../lib/session";
import type { LoginResponse } from "../lib/types";

function Logo() {
  return (
    <svg width="44" height="44" viewBox="0 0 40 40" aria-hidden="true">
      <rect width="40" height="40" rx="9" fill="#0f7c91" />
      <path
        d="M7 26c4 0 4-6 8-6s4 6 8 6 4-6 8-6"
        fill="none"
        stroke="#ffffff"
        strokeWidth="2.6"
        strokeLinecap="round"
      />
      <path d="M20 9v8M16 13h8" stroke="#bfe7ee" strokeWidth="2.4" strokeLinecap="round" />
    </svg>
  );
}

export default function LoginPage() {
  const router = useRouter();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError("");
    setBusy(true);
    try {
      const res = await fetch("/api/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, password }),
      });
      const data = (await res.json()) as Partial<LoginResponse> & { error?: string };
      if (!res.ok) {
        setError(data.error || "Invalid username or password.");
        setBusy(false);
        return;
      }

      // w8-planner-2 MFA rollout: password was correct, but a full session
      // was not issued — an in-scope account under enforce mode must
      // complete either forced enrollment or a login challenge first. See
      // lib/session.ts's PendingMfaChallenge doc comment for what this
      // token does and does not prove.
      //
      // Round-1 review (B01): a browser holding a stale session for one
      // account must never ALSO hold a pending challenge for a different
      // login — mfa/enroll's post() attaches Authorization whenever a
      // token exists, and a leftover session from a previous (or
      // abandoned) sign-in would then reach the gateway alongside this
      // new challenge_token. Clearing the old session before storing the
      // new challenge closes that off at the source, not just at the
      // gateway (see app.py::_resolve_mfa_principal's own fail-closed
      // fix for the same finding).
      if (!data.token && data.mfa?.challenge_token) {
        clearSession();
        clearPendingMfaChallenge();
        setPendingMfaChallenge(data.mfa.challenge_token, Boolean(data.mfa.enrollment_required));
        router.replace(data.mfa.enrollment_required ? "/mfa/enroll" : "/login/mfa");
        return;
      }

      if (!data.token || !data.user) {
        setError("Invalid username or password.");
        setBusy(false);
        return;
      }

      // Symmetric with the branch above: a full session must not coexist
      // with a stale pending challenge left over from an earlier attempt.
      clearPendingMfaChallenge();
      setSession(data.token, data.user);

      // Prompt mode never blocks login — this is a nudge, not a gate. An
      // unenrolled in-scope account lands on the enrollment flow with a
      // "skip for now" way out; every other case goes straight to the
      // dashboard exactly as before MFA existed.
      if (data.mfa?.prompt) {
        router.replace("/mfa/enroll?voluntary=1");
        return;
      }
      router.replace("/");
    } catch {
      setError("Could not reach the portal. Please try again.");
      setBusy(false);
    }
  }

  return (
    <div className="rb-login">
      <main className="rb-login__card" id="rb-main">
        <div className="rb-login__brand">
          <Logo />
          <div>
            <div className="rb-login__brand-name">Riverbend Community Health</div>
            <div className="rb-login__brand-tag">PATIENT PORTAL</div>
          </div>
        </div>

        <h1 className="rb-login__title">Sign in</h1>
        <p className="rb-login__sub">Access your appointments, records, and forms.</p>

        {error && (
          <div className="rb-alert rb-alert--err" role="alert">
            {error}
          </div>
        )}

        <form onSubmit={submit}>
          <div className="rb-field">
            <label className="rb-field__label" htmlFor="login-username">
              Username
            </label>
            <input
              id="login-username"
              className="rb-input"
              autoComplete="username"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              required
            />
          </div>

          <div className="rb-field">
            <label className="rb-field__label" htmlFor="login-password">
              Password
            </label>
            <input
              id="login-password"
              className="rb-input"
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
            />
          </div>

          <button className="rb-btn rb-btn--primary rb-btn--block" disabled={busy} type="submit">
            {busy ? (
              <>
                <span className="rb-spinner" aria-hidden="true" /> Signing in…
              </>
            ) : (
              "Sign in"
            )}
          </button>
        </form>

        <div className="rb-login__hint">
          <strong>Demo access:</strong> sign in with <code>frontdesk</code> /{" "}
          <code>portal123</code>.
        </div>

        <div className="rb-login__footer">
          Need help? Call the Riverbend front desk at (555) 014-2200.
          <br />
          © 2020 Riverbend Community Health. v0.9.3
        </div>
      </main>
    </div>
  );
}
