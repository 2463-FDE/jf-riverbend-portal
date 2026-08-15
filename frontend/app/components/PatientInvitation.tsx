"use client";

import { useState } from "react";
import { apiFetch } from "../lib/session";

/**
 * Front-desk issuance of a patient portal invitation.
 *
 * The code is shown once and is never retrievable afterwards — the gateway
 * stores only a hash, because an invitation code is a credential for a medical
 * record. That constraint drives the whole design of this component: the code
 * is displayed large enough to read aloud across a desk, grouped for
 * transcription, with an explicit warning that closing the panel loses it, and
 * a copy button so it does not have to be retyped.
 */
export default function PatientInvitation({ patientId }: { patientId: string }) {
  const [code, setCode] = useState<string | null>(null);
  const [expiresAt, setExpiresAt] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState(false);

  async function issue() {
    setError(null);
    setBusy(true);
    try {
      const res = await apiFetch(`/api/patients/${patientId}/invitation`, {
        method: "POST",
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) {
        // 409 is the useful one: this patient already has a live invitation.
        // Say what to do about it rather than showing a status code.
        setError(
          res.status === 409
            ? "This patient already has an active invitation. Revoke it before issuing another."
            : body?.detail || "Could not issue an invitation. Please try again."
        );
        return;
      }
      setCode(body.code);
      setExpiresAt(body.expires_at ?? null);
    } catch {
      setError("Could not reach the server. Please try again.");
    } finally {
      setBusy(false);
    }
  }

  async function copy() {
    if (!code) return;
    try {
      await navigator.clipboard.writeText(code);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // Clipboard can be blocked by permissions; the code is on screen anyway.
      setCopied(false);
    }
  }

  if (code) {
    return (
      <div className="rb-invite" role="status">
        <h3 className="rb-invite__title">Portal invitation for this patient</h3>

        <p className="rb-invite__warn">
          <strong>Write this down or give it to the patient now.</strong> It is
          shown only once and cannot be looked up again. If it is lost, revoke
          this invitation and issue a new one.
        </p>

        <div className="rb-invite__code" aria-label="Invitation code">
          {code}
        </div>

        <div className="rb-invite__actions">
          <button type="button" onClick={copy} className="rb-btn">
            {copied ? "Copied" : "Copy code"}
          </button>
          {expiresAt && (
            <span className="rb-invite__expiry">
              Expires {new Date(expiresAt).toLocaleDateString()}
            </span>
          )}
        </div>

        <p className="rb-invite__hint">
          The patient activates it at <strong>/activate</strong> and chooses
          their own password. They will only ever see their own record.
        </p>
      </div>
    );
  }

  return (
    <div className="rb-invite">
      <h3 className="rb-invite__title">Patient portal access</h3>
      <p className="rb-invite__hint">
        Issues a one-time code the patient uses to set up their portal sign-in.
        Give it to them in person — it is their key to their own record.
      </p>
      {error && (
        <p className="rb-invite__error" role="alert">
          {error}
        </p>
      )}
      <button type="button" onClick={issue} disabled={busy} className="rb-btn">
        {busy ? "Issuing…" : "Issue invitation"}
      </button>
    </div>
  );
}
