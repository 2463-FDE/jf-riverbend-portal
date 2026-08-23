"use client";

import { useState } from "react";
import PatientName from "./PatientName";
import { apiFetch } from "../lib/session";

/**
 * The clinician's view of an AI draft: generate, read it in full, decide.
 *
 * Sits beside the existing review queue rather than inside it — the queue
 * decides whether a RECORD's own words are released, this decides whether a
 * GENERATED summary is. Approving text you have not read is the failure both
 * screens exist to prevent, so the full draft is always shown, never a summary
 * of it, and the provenance label is shown next to it so a fallback is never
 * approved in the belief that a model wrote it.
 */

interface Citation {
  source_id: string;
  source_version: string;
  citation_id: string;
  category: string | null;
}

interface Draft {
  id: number;
  patient_id: number;
  version: number;
  status: string;
  provenance_label: string;
  model_id: string | null;
  validation_code: string | null;
  generated_text: string;
  citations: Citation[];
}

export default function AgentDraftPanel() {
  const [patientId, setPatientId] = useState("1737");
  // The identity box needs the LOADED id, same rule as records/appointments:
  // it only advances once Load/Generate actually runs, never on a keystroke —
  // and starts EMPTY, not "1737", so this panel makes no patient-specific
  // request at all until a clinician actually presses one of those buttons.
  const [loadedPatientId, setLoadedPatientId] = useState("");
  const [draft, setDraft] = useState<Draft | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function call(path: string, init?: RequestInit) {
    setBusy(true);
    setNote(null);
    try {
      const res = await apiFetch(path, init);
      if (res.status === 401 || res.status === 403) {
        setDraft(null);
        setNote("You do not have access to drafts for this patient.");
        return;
      }
      if (res.status === 404) {
        setDraft(null);
        setNote("No draft exists for this patient yet.");
        return;
      }
      if (!res.ok) {
        setNote("That did not work. Please try again shortly.");
        return;
      }
      setDraft((await res.json()) as Draft);
    } catch {
      setNote("We could not reach the server.");
    } finally {
      setBusy(false);
    }
  }

  const base = `/api/patients/${encodeURIComponent(patientId)}/agent-draft`;

  async function decide(decision: "approved" | "rejected") {
    if (!draft) return;
    await call(`/api/agent-drafts/${draft.id}/decision`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ decision }),
    });
  }

  return (
    <section className="rb-agent-draft" aria-labelledby="agent-draft-heading">
      <h2 id="agent-draft-heading">AI summary draft</h2>

      <div className="rb-field" style={{ maxWidth: 820 }}>
        <label className="rb-field__label" htmlFor="agent-draft-patient">
          Patient ID
        </label>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <input
            id="agent-draft-patient"
            className="rb-input"
            style={{ flex: "0 1 160px" }}
            value={patientId}
            onChange={(e) => {
              setPatientId(e.target.value);
              setDraft(null);
              setNote(null);
            }}
            inputMode="numeric"
          />
          <button
            type="button"
            className="rb-btn"
            style={{ flex: "0 0 112px" }}
            disabled={busy}
            onClick={() => { setLoadedPatientId(patientId); void call(base); }}
          >
            Load
          </button>
          <button
            type="button"
            className="rb-btn rb-btn--primary"
            style={{ flex: "0 0 112px" }}
            disabled={busy}
            onClick={() => { setLoadedPatientId(patientId); void call(base, { method: "POST" }); }}
          >
            Generate
          </button>
          {/* Fixed wider than PatientName's own 260px default so this
              panel's names never truncate — the wrapper, not PatientName
              itself, carries the flex sizing here, since PatientName's own
              inline flex only takes effect as a direct flex child; every
              other caller keeps the narrower shared default. */}
          <div style={{ flex: "0 1 340px" }}>
            <PatientName patientId={loadedPatientId} />
          </div>
        </div>
      </div>

      {note && <p role="status">{note}</p>}

      {draft && (
        <div className="rb-agent-draft__body">
          <p className="rb-agent-draft__meta">
            Version {draft.version} · <span data-status={draft.status}>{draft.status}</span> ·{" "}
            <span className="rb-agent-label" data-provenance={draft.provenance_label}>
              {draft.provenance_label}
            </span>
            {draft.validation_code ? ` · ${draft.validation_code}` : ""}
          </p>

          <p className="rb-agent-draft__text">{draft.generated_text}</p>

          {draft.citations.length > 0 && (
            <ul className="rb-agent-citations">
              {draft.citations.map((c) => (
                <li key={c.citation_id}>
                  {c.citation_id}
                  {c.category ? ` (${c.category})` : ""}
                </li>
              ))}
            </ul>
          )}

          {draft.status === "validated" ? (
            <div className="rb-review-actions">
              <button type="button" className="rb-btn" disabled={busy} onClick={() => void decide("approved")}>
                Approve for patient
              </button>
              <button type="button" className="rb-btn" disabled={busy} onClick={() => void decide("rejected")}>
                Reject
              </button>
            </div>
          ) : (
            <p className="rb-muted">
              Only a validated draft can be decided. This one is {draft.status}.
            </p>
          )}
        </div>
      )}
    </section>
  );
}
