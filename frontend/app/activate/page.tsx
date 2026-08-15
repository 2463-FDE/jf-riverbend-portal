"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";

const MIN_PASSWORD = 12;

/**
 * Where a patient redeems the code the front desk gave them.
 *
 * Public by necessity — they have no account yet. Two things this page must
 * not do: explain WHY a code was rejected (the gateway deliberately returns one
 * generic answer so the endpoint cannot be used to discover valid codes), and
 * suggest the code can be recovered (it cannot; only a new one can be issued).
 */
export default function ActivatePage() {
  const router = useRouter();
  const [code, setCode] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);

    // Checked here so a mistyped confirmation never spends the code.
    if (password !== confirm) {
      setError("The two passwords do not match.");
      return;
    }
    if (password.length < MIN_PASSWORD) {
      setError(`Choose a password of at least ${MIN_PASSWORD} characters.`);
      return;
    }

    setBusy(true);
    try {
      const res = await fetch("/api/patient/activate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code, password }),
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) {
        setError(body?.detail || "We could not activate your account.");
        return;
      }
      setDone(body.username);
    } catch {
      setError("We could not reach the server. Please try again.");
    } finally {
      setBusy(false);
    }
  }

  if (done) {
    return (
      <div className="rb-login"><main className="rb-login__card" id="rb-main">
        <h1 className="rb-login__title">Your account is ready</h1>
        <p>
          Sign in with the username <strong>{done}</strong> and the password you
          just chose.
        </p>
        <button type="button" className="rb-btn" onClick={() => router.push("/login")}>
          Go to sign in
        </button>
      </main></div>
    );
  }

  return (
    <div className="rb-login"><main className="rb-login__card" id="rb-main">
      <h1 className="rb-login__title">Set up your portal access</h1>
      <p className="rb-login__sub">
        Enter the code your clinic gave you, then choose a password. You will
        only ever see your own record.
      </p>

      <form onSubmit={submit}>
        <div className="rb-field">
        <label className="rb-field__label" htmlFor="code">Invitation code</label>
        <input
          id="code"
          className="rb-input"
          value={code}
          onChange={(e) => setCode(e.target.value)}
          placeholder="ABCD-EFGH-JKMN-PQRS"
          autoComplete="one-time-code"
          spellCheck={false}
          required
        />
        </div>

        <div className="rb-field">
        <label className="rb-field__label" htmlFor="password">Choose a password</label>
        <input
          id="password"
          className="rb-input"
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          autoComplete="new-password"
          minLength={MIN_PASSWORD}
          required
        />
        </div>

        <div className="rb-field">
        <label className="rb-field__label" htmlFor="confirm">Confirm password</label>
        <input
          id="confirm"
          className="rb-input"
          type="password"
          value={confirm}
          onChange={(e) => setConfirm(e.target.value)}
          autoComplete="new-password"
          required
        />
        </div>

        {error && (
          <div className="rb-alert rb-alert--err" role="alert">
            {error}
          </div>
        )}

        <button type="submit" className="rb-btn" disabled={busy}>
          {busy ? "Activating…" : "Activate my account"}
        </button>
      </form>

      <p className="rb-login__sub">
        If your code does not work, contact the clinic — they can issue a new
        one. A code can only be used once.
      </p>
    </main></div>
  );
}
