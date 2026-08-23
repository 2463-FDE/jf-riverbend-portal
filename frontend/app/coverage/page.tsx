"use client";

import { useRef, useState } from "react";
import Card from "../components/Card";
import PatientName from "../components/PatientName";
import StatusBadge from "../components/StatusBadge";
import { IconBilling } from "../components/icons";
import { apiFetch } from "../lib/session";
import type { CoverageItem, EligibilityResult } from "../lib/types";
import { fmtDateTime } from "../lib/format";

/**
 * Coverage & Eligibility (W9.3) — replaces the disabled Billing placeholder.
 *
 * Deliberately named away from "Billing": this repository has coverage and
 * eligibility capabilities, and nothing else — no claims, invoices,
 * balances, or payments exist anywhere in the schema. A screen with the
 * word "Billing" on it would promise those before they are ever built.
 *
 * Every verify/status/retry call is scoped through the coverage's own
 * patient_id server-side (services/gateway/app.py's coverage/eligibility
 * routes) — this page never sees or sends a raw eligibility job id, only
 * the safe category the gateway maps a job's state into.
 */

const CATEGORY_LABEL: Record<string, string> = {
  active: "Active",
  inactive: "Inactive",
  unknown: "Unknown",
  stale: "Stale — verify again",
  pending: "Checking…",
  simulated: "Synthetic training — no payer contacted",
  unavailable: "Temporarily unavailable",
};

const CATEGORY_STATUS: Record<string, string> = {
  active: "active",
  inactive: "inactive",
  unknown: "neutral",
  stale: "pending",
  pending: "pending",
  simulated: "booking",
  unavailable: "denied",
};

function isValidPatientId(id: string): boolean {
  return /^\d+$/.test(id.trim());
}

export default function CoveragePage() {
  const [patientId, setPatientId] = useState("");
  const [loadedPatientId, setLoadedPatientId] = useState("");
  const loadedPatientIdRef = useRef(loadedPatientId);
  loadedPatientIdRef.current = loadedPatientId;

  const [coverages, setCoverages] = useState<CoverageItem[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [results, setResults] = useState<Record<number, EligibilityResult>>({});
  const [actionBusy, setActionBusy] = useState<number | null>(null);

  function handlePatientIdChange(value: string) {
    setPatientId(value);
    setLoadedPatientId("");
    setCoverages(null);
    setResults({});
    setError(null);
  }

  async function load() {
    if (!isValidPatientId(patientId)) return;
    const requestedId = patientId;
    setBusy(true);
    setError(null);
    setLoadedPatientId(requestedId);
    setCoverages(null);
    setResults({});
    try {
      const res = await apiFetch(`/api/patients/${encodeURIComponent(requestedId)}/coverages`);
      const body = await res.json().catch(() => ({}));
      if (loadedPatientIdRef.current !== requestedId) return;
      if (!res.ok) {
        setError(
          res.status === 403
            ? "You are not authorized to view this patient's coverage."
            : "Could not load coverage. Please try again."
        );
        return;
      }
      setCoverages(Array.isArray(body.items) ? body.items : []);
    } catch {
      if (loadedPatientIdRef.current === requestedId) {
        setError("We could not reach the server.");
      }
    } finally {
      if (loadedPatientIdRef.current === requestedId) setBusy(false);
    }
  }

  async function verify(coverageId: number) {
    setActionBusy(coverageId);
    try {
      const res = await apiFetch(`/api/patients/${loadedPatientId}/coverages/${coverageId}/verify`, {
        method: "POST",
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) {
        setResults((r) => ({ ...r, [coverageId]: { category: "unavailable", message: "Could not request a verification." } }));
        return;
      }
      setResults((r) => ({ ...r, [coverageId]: body }));
    } catch {
      setResults((r) => ({ ...r, [coverageId]: { category: "unavailable", message: "We could not reach the server." } }));
    } finally {
      setActionBusy(null);
    }
  }

  async function checkStatus(coverageId: number) {
    setActionBusy(coverageId);
    try {
      const res = await apiFetch(`/api/patients/${loadedPatientId}/coverages/${coverageId}/eligibility-status`);
      const body = await res.json().catch(() => ({}));
      if (res.ok) setResults((r) => ({ ...r, [coverageId]: body }));
    } finally {
      setActionBusy(null);
    }
  }

  async function retry(coverageId: number) {
    setActionBusy(coverageId);
    try {
      const res = await apiFetch(`/api/patients/${loadedPatientId}/coverages/${coverageId}/eligibility-retry`, {
        method: "POST",
      });
      const body = await res.json().catch(() => ({}));
      if (res.ok) setResults((r) => ({ ...r, [coverageId]: body }));
    } finally {
      setActionBusy(null);
    }
  }

  return (
    <div className="rb-stack">
      <div className="rb-page-head">
        <h1>Coverage &amp; Eligibility</h1>
        <p>Look up a patient&apos;s coverage and request a payer eligibility check.</p>
      </div>

      <Card>
        <div className="rb-field" style={{ marginBottom: 0 }}>
          <label className="rb-field__label" htmlFor="coverage-patient">
            Patient ID
          </label>
          <div style={{ display: "flex", gap: 8, alignItems: "center", maxWidth: 600 }}>
            <input
              id="coverage-patient"
              className="rb-input"
              style={{ flex: "0 1 160px" }}
              placeholder="Patient ID"
              value={patientId}
              onChange={(e) => handlePatientIdChange(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && void load()}
              inputMode="numeric"
            />
            <PatientName patientId={loadedPatientId} nameOnly />
            <button className="rb-btn rb-btn--primary" onClick={() => void load()} disabled={busy} type="button">
              {busy ? "Loading…" : "Load"}
            </button>
          </div>
        </div>
      </Card>

      {error && (
        <div className="rb-alert rb-alert--err" role="alert">
          {error}
        </div>
      )}

      {coverages && coverages.length === 0 && (
        <div className="rb-empty">No coverage on file for this patient.</div>
      )}

      {coverages && coverages.length > 0 && (
        <div className="rb-list">
          {coverages.map((c) => {
            const result = results[c.id];
            const category = result?.category;
            const busyHere = actionBusy === c.id;
            return (
              <Card key={c.id} icon={<IconBilling />} title={c.payer_name ?? "Coverage on file"}>
                <div className="rb-listrow__meta">
                  {c.plan_type && <span>{c.plan_type}</span>}
                  {c.member_id_masked && <span>Member ID {c.member_id_masked}</span>}
                  {c.group_number && <span>Group {c.group_number}</span>}
                  {c.status && <StatusBadge status={c.status} />}
                  {c.verified_at && <span>Last verified {fmtDateTime(c.verified_at)}</span>}
                </div>

                {!c.has_member_id && (
                  <p className="rb-muted" style={{ marginTop: 8 }}>
                    No member id on file — a verification cannot be requested.
                  </p>
                )}

                {category && (
                  <p style={{ marginTop: 10 }}>
                    <StatusBadge status={CATEGORY_STATUS[category] ?? "neutral"} label={CATEGORY_LABEL[category] ?? category} />
                    {result?.message && <span className="rb-muted" style={{ marginLeft: 8 }}>{result.message}</span>}
                  </p>
                )}

                <div style={{ display: "flex", gap: 8, marginTop: 12 }}>
                  <button
                    type="button"
                    className="rb-btn rb-btn--primary"
                    disabled={busyHere || !c.has_member_id}
                    onClick={() => void verify(c.id)}
                  >
                    {busyHere ? "Working…" : "Request verification"}
                  </button>
                  <button type="button" className="rb-btn" disabled={busyHere} onClick={() => void checkStatus(c.id)}>
                    Check status
                  </button>
                  {result?.can_retry && (
                    <button type="button" className="rb-btn" disabled={busyHere} onClick={() => void retry(c.id)}>
                      Retry
                    </button>
                  )}
                </div>
              </Card>
            );
          })}
        </div>
      )}
    </div>
  );
}
