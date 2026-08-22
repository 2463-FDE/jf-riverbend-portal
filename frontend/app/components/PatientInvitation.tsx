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
  // Blank or non-numeric — this component has no id of its own to validate
  // against, only whatever the records screen's Patient ID field currently
  // holds, which starts empty now that screen has no default (2026-08-22).
  const validId = /^\d+$/.test(patientId.trim());
  const [code, setCode] = useState<string | null>(null);
  const [expiresAt, setExpiresAt] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState(false);
  const [revoked, setRevoked] = useState(false);
  const [blocked, setBlocked] = useState(false);

  async function issue() {
    if (!validId) return;
    setError(null);
    setBusy(true);
    try {
      const res = await apiFetch(`/api/patients/${patientId}/invitation`, {
        method: "POST",
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) {
        // Branch on the machine-readable reason, never on the English
        // message — a 409 is not ONE thing. LIVE_INVITATION means a revoke
        // control is the useful next action; ACTIVE_PORTAL_ACCOUNT means
        // there is no invitation to revoke at all, and offering that button
        // would be an action with nothing behind it.
        const reason = body?.detail?.reason;
        setBlocked(reason === "LIVE_INVITATION");
        if (reason === "LIVE_INVITATION") {
          setError("This patient already has an unexpired invitation.");
        } else if (reason === "ACTIVE_PORTAL_ACCOUNT") {
          setError("This patient already has an active portal account.");
        } else {
          setError(
            typeof body?.detail === "string"
              ? body.detail
              : body?.detail?.message || "Could not issue an invitation. Please try again."
          );
        }
        return;
      }
      setBlocked(false);
      setCode(body.code);
      setExpiresAt(body.expires_at ?? null);
    } catch {
      setError("Could not reach the server. Please try again.");
    } finally {
      setBusy(false);
    }
  }

  // Offered only after a 409, which is the moment it is actually useful: the
  // front desk has been told an unexpired invitation is in the way, and this
  // is what clears it. Deliberately not a permanent button on this panel —
  // revoking is a corrective action, not part of ordinary registration.
  async function revoke() {
    setError(null);
    setBusy(true);
    try {
      const res = await apiFetch(`/api/patients/${patientId}/invitation`, {
        method: "DELETE",
      });
      if (!res.ok) {
        setError("Could not revoke the existing invitation. Please try again.");
        return;
      }
      setRevoked(true);
      setBlocked(false);
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
      {revoked && !error && (
        <p className="rb-invite__revoked" role="status">
          The previous invitation was revoked. You can issue a new one now.
        </p>
      )}
      <div className="rb-invite__actions">
        <button type="button" onClick={issue} disabled={busy || !validId} className="rb-btn">
          {busy ? "Issuing…" : "Issue invitation"}
        </button>
        {blocked && (
          <button type="button" onClick={revoke} disabled={busy} className="rb-btn">
            {busy ? "Revoking…" : "Revoke existing invitation"}
          </button>
        )}
      </div>
    </div>
  );
}
