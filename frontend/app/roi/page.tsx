"use client";

import { useCallback, useEffect, useState } from "react";
import Card from "../components/Card";
import StatusBadge from "../components/StatusBadge";
import { IconRoi, IconPlus } from "../components/icons";
import { apiFetch } from "../lib/session";
import type { RoiRequest } from "../lib/types";
import { fmtDate } from "../lib/format";

const DEFAULT_PATIENT_ID = "1042";

const RECIPIENT_TYPES = [
  "Healthcare provider",
  "Insurance company",
  "Attorney",
  "Employer",
  "Patient / personal",
  "Government agency",
  "Other",
];

const PURPOSES = [
  "Continuity of care",
  "Insurance claim",
  "Legal proceeding",
  "Personal records",
  "Disability / FMLA",
  "Second opinion",
  "Other",
];

export default function RoiPage() {
  const [patientId, setPatientId] = useState(DEFAULT_PATIENT_ID);
  const [recipient, setRecipient] = useState("");
  const [recipientType, setRecipientType] = useState(RECIPIENT_TYPES[0]);
  const [purpose, setPurpose] = useState(PURPOSES[0]);
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");

  const [requests, setRequests] = useState<RoiRequest[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [busyFulfill, setBusyFulfill] = useState<number | null>(null);
  const [msg, setMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(null);
  // W10 Final 2 Stage 1: fulfillment requires a specific, already-reviewed
  // authorization id (roi-service's FulfillRequest.authorization_id) — there
  // is no authorization-review UI yet (that workflow is API-only today), so
  // staff enter the id of the authorization a supervisor already reviewed
  // 'valid' for this patient/recipient. Keyed per request id so multiple
  // pending rows don't share one input.
  const [authorizationIds, setAuthorizationIds] = useState<Record<number, string>>({});

  const load = useCallback(async () => {
    setRequests(null);
    try {
      const r = await apiFetch(`/api/roi/requests?patient_id=${encodeURIComponent(patientId)}`);
      const d = await r.json();
      setRequests(Array.isArray(d) ? d : (d.items ?? []));
    } catch {
      setRequests([]);
    }
  }, [patientId]);

  useEffect(() => {
    load();
  }, [load]);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setMsg(null);
    try {
      const r = await apiFetch("/api/roi/requests", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          patient_id: Number(patientId) || patientId,
          recipient,
          recipient_type: recipientType,
          purpose,
          date_range_start: start,
          date_range_end: end,
        }),
      });
      if (!r.ok) throw new Error();
      setMsg({ kind: "ok", text: "Records release request submitted." });
      setRecipient("");
      setStart("");
      setEnd("");
      await load();
    } catch {
      setMsg({ kind: "err", text: "Could not submit the request. Please try again." });
    } finally {
      setBusy(false);
    }
  }

  async function fulfill(req: RoiRequest) {
    const raw = authorizationIds[req.id] ?? "";
    const authorizationId = Number(raw);
    if (!raw || !Number.isInteger(authorizationId) || authorizationId <= 0) {
      setMsg({ kind: "err", text: "Enter the id of a reviewed, valid authorization before fulfilling." });
      return;
    }
    setBusyFulfill(req.id);
    setMsg(null);
    try {
      const r = await apiFetch(`/api/roi/requests/${req.id}/fulfill`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ authorization_id: authorizationId }),
      });
      const data = await r.json().catch(() => null);
      if (!r.ok) {
        // Truthful failure text — the gateway now forwards roi-service's
        // real status/detail (e.g. "authorization has expired", "an active
        // disclosure restriction blocks this release") instead of a
        // downstream rejection silently becoming a false 200.
        const detail = typeof data?.detail === "string" ? data.detail : null;
        setMsg({ kind: "err", text: detail || "Could not fulfill that request." });
        return;
      }
      setMsg({ kind: "ok", text: `Request #${req.id} marked fulfilled.` });
      await load();
    } catch {
      setMsg({ kind: "err", text: "Could not fulfill that request." });
    } finally {
      setBusyFulfill(null);
    }
  }

  return (
    <div className="rb-stack">
      <div className="rb-page-head">
        <h1>Release of Information</h1>
        <p>Request that your health records be released to a third party.</p>
      </div>

      {msg && (
        <div className={`rb-alert rb-alert--${msg.kind === "ok" ? "ok" : "err"}`} role="status">
          {msg.text}
        </div>
      )}

      <div className="rb-grid rb-grid--2">
        <Card title="New release request" icon={<IconPlus />}>
          <form onSubmit={submit}>
            <div className="rb-field">
              <label className="rb-field__label" htmlFor="roi-patient">
                Patient ID<span className="rb-field__req" aria-hidden="true">*</span>
              </label>
              <input id="roi-patient" className="rb-input" value={patientId}
                onChange={(e) => setPatientId(e.target.value)} required inputMode="numeric" />
            </div>

            <div className="rb-field">
              <label className="rb-field__label" htmlFor="roi-recipient">
                Recipient<span className="rb-field__req" aria-hidden="true">*</span>
              </label>
              <input id="roi-recipient" className="rb-input" value={recipient}
                onChange={(e) => setRecipient(e.target.value)} required
                placeholder="" />
              <span className="rb-field__hint">Name of the person or organization receiving the records.</span>
            </div>

            <div className="rb-field-row">
              <div className="rb-field">
                <label className="rb-field__label" htmlFor="roi-rtype">Recipient type</label>
                <select id="roi-rtype" className="rb-select" value={recipientType}
                  onChange={(e) => setRecipientType(e.target.value)}>
                  {RECIPIENT_TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
                </select>
              </div>
              <div className="rb-field">
                <label className="rb-field__label" htmlFor="roi-purpose">Purpose</label>
                <select id="roi-purpose" className="rb-select" value={purpose}
                  onChange={(e) => setPurpose(e.target.value)}>
                  {PURPOSES.map((t) => <option key={t} value={t}>{t}</option>)}
                </select>
              </div>
            </div>

            <div className="rb-field-row">
              <div className="rb-field">
                <label className="rb-field__label" htmlFor="roi-start">Records from</label>
                <input id="roi-start" className="rb-input" type="date" value={start}
                  onChange={(e) => setStart(e.target.value)} />
              </div>
              <div className="rb-field">
                <label className="rb-field__label" htmlFor="roi-end">Records to</label>
                <input id="roi-end" className="rb-input" type="date" value={end}
                  onChange={(e) => setEnd(e.target.value)} />
              </div>
            </div>

            <button className="rb-btn rb-btn--primary rb-btn--block" disabled={busy} type="submit">
              {busy ? (
                <><span className="rb-spinner" aria-hidden="true" /> Submitting…</>
              ) : (
                "Submit request"
              )}
            </button>
          </form>
        </Card>

        <Card title="Existing requests" icon={<IconRoi />}
          action={<button className="rb-btn rb-btn--ghost rb-btn--sm" onClick={load} type="button">Refresh</button>}>
          {requests === null ? (
            <div className="rb-loading">
              <span className="rb-spinner rb-spinner--dark" aria-hidden="true" /> Loading requests…
            </div>
          ) : requests.length ? (
            <div className="rb-list">
              {requests.map((req) => {
                const done = ["fulfilled", "completed", "denied"].includes(req.status?.toLowerCase());
                return (
                  <div className="rb-listrow" key={req.id} style={{ display: "block" }}>
                    <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                      <div className="rb-listrow__title" style={{ flex: 1 }}>
                        {req.recipient || "Recipient"} <span className="rb-muted">· #{req.id}</span>
                      </div>
                      <StatusBadge status={req.status} />
                    </div>
                    <div className="rb-listrow__meta" style={{ marginTop: 6 }}>
                      <span>{req.recipient_type}</span>
                      <span>{req.purpose}</span>
                      {(req.date_range_start || req.date_range_end) && (
                        <span>{fmtDate(req.date_range_start)} – {fmtDate(req.date_range_end)}</span>
                      )}
                    </div>
                    {!done && (
                      <div style={{ marginTop: 10, display: "flex", alignItems: "center", gap: 8 }}>
                        <input
                          className="rb-input"
                          style={{ width: 140 }}
                          placeholder="Authorization ID"
                          inputMode="numeric"
                          aria-label={`Authorization ID for request #${req.id}`}
                          value={authorizationIds[req.id] ?? ""}
                          onChange={(e) =>
                            setAuthorizationIds((prev) => ({ ...prev, [req.id]: e.target.value }))
                          }
                        />
                        <button
                          className="rb-btn rb-btn--sm"
                          onClick={() => fulfill(req)}
                          disabled={busyFulfill === req.id}
                          type="button"
                        >
                          {busyFulfill === req.id ? "Fulfilling…" : "Fulfill"}
                        </button>
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          ) : (
            <div className="rb-empty">No release requests on file for this patient.</div>
          )}
        </Card>
      </div>
    </div>
  );
}
