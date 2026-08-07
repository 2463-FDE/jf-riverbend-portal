"use client";

import { useRef, useState } from "react";
import Card from "../components/Card";
import StatusBadge, { statusVariant } from "../components/StatusBadge";
import { IconRecords, IconLab, IconSearch, IconStethoscope } from "../components/icons";
import { apiFetch } from "../lib/session";
import type { EncounterBlock, PatientViewResult, ReconciliationResult, RecordItem } from "../lib/types";
import { fmtDate } from "../lib/format";

function isResult(r: RecordItem): boolean {
  return Boolean(r.test || r.value !== undefined || r.reference_range);
}

const AI_HEADING: Record<PatientViewResult["outcome"], string> = {
  completed: "Chart summary",
  escalated: "Chart summary — clinician review needed",
  refused: "Request refused",
};

const AI_CALLOUT: Record<"escalated" | "refused", string> = {
  escalated:
    "Treat this summary as a starting point, not a final read of the chart — verify details against the source record before relying on it.",
  refused: "No chart content is shown here. Check the source record directly.",
};

export default function RecordsPage() {
  // The records view loads by a patient id taken straight off the input/URL.
  // The id is a sequential integer and the backend does NOT check ownership
  // (IDOR — intentional teaching point; see docs/handover/portal.har). We pass
  // whatever id is entered straight through to /api/records.
  const [patientId, setPatientId] = useState("1042");
  // Mirrors patientId for reads inside already-in-flight async callbacks —
  // state captured by a closure at call time can't see a later change, and
  // an in-flight fetch has to know if the id moved on before it applies its
  // response (see handlePatientIdChange below).
  const patientIdRef = useRef(patientId);
  patientIdRef.current = patientId;

  const [data, setData] = useState<EncounterBlock[] | null>(null);
  const [selected, setSelected] = useState<EncounterBlock | null>(null);
  const [status, setStatus] = useState("");
  const [busy, setBusy] = useState(false);

  // Stage 3 — bounded, evidence-cited AI chart view (libs.patient_view_agent
  // via GET /api/patients/[id]/view). Separate from the plain records load
  // above: this is gated by an authenticated-staff access check, not a
  // patient-specific one — see the route's own comments.
  const [aiView, setAiView] = useState<PatientViewResult | null>(null);
  const [aiStatus, setAiStatus] = useState("");
  const [aiBusy, setAiBusy] = useState(false);

  // Stage 2 (Week 6) — read-only "possible duplicate patient" reconciliation
  // check (GET /api/patients/[id]/reconciliation). Its own on-demand button
  // and its own audit trail — a distinct, more sensitive read than the AI
  // chart view above, so it is never auto-fetched.
  const [reconciliation, setReconciliation] = useState<ReconciliationResult | null>(null);
  const [reconciliationStatus, setReconciliationStatus] = useState("");
  const [reconciliationBusy, setReconciliationBusy] = useState(false);

  // None of the three reads on this page bind their result to a specific
  // caller/patient relationship server-side (see RIV-201 and the comments
  // on each load* function below) — so the ONLY thing standing between a
  // clinician and reading a different patient's data under the wrong
  // heading is this client clearing stale panels the moment the id changes.
  // Every keystroke drops all three results immediately; the load* functions
  // additionally guard against a slow response for the OLD id landing after
  // the id has already moved on (see patientIdRef checks below).
  function handlePatientIdChange(value: string) {
    setPatientId(value);
    setData(null);
    setSelected(null);
    setStatus("");
    setAiView(null);
    setAiStatus("");
    setReconciliation(null);
    setReconciliationStatus("");
  }

  async function loadReconciliation() {
    const requestedId = patientId;
    setReconciliationBusy(true);
    setReconciliationStatus("");
    setReconciliation(null);
    try {
      const res = await apiFetch(`/api/patients/${encodeURIComponent(requestedId)}/reconciliation`);
      const json = await res.json();
      if (patientIdRef.current !== requestedId) return; // patient changed while this was in flight
      if (!res.ok) {
        const detail = json?.detail;
        const reason = typeof detail === "string" ? detail : detail?.reason;
        setReconciliationStatus(
          reason ? `Could not check for related records: ${reason}` : "Could not check for related records."
        );
        return;
      }
      setReconciliation(json as ReconciliationResult);
    } catch (e) {
      if (patientIdRef.current === requestedId) {
        setReconciliationStatus(e instanceof Error ? e.message : "Could not check for related records.");
      }
    } finally {
      setReconciliationBusy(false);
    }
  }

  async function loadAiView() {
    const requestedId = patientId;
    setAiBusy(true);
    setAiStatus("");
    setAiView(null);
    try {
      const res = await apiFetch(`/api/patients/${encodeURIComponent(requestedId)}/view`);
      const json = await res.json();
      if (patientIdRef.current !== requestedId) return; // patient changed while this was in flight
      if (!res.ok) {
        const detail = json?.detail;
        const reason = typeof detail === "string" ? detail : detail?.reason;
        setAiStatus(reason ? `Could not load AI chart view: ${reason}` : "Could not load AI chart view.");
        return;
      }
      setAiView(json as PatientViewResult);
    } catch (e) {
      if (patientIdRef.current === requestedId) {
        setAiStatus(e instanceof Error ? e.message : "Could not load AI chart view.");
      }
    } finally {
      setAiBusy(false);
    }
  }

  async function load() {
    const requestedId = patientId;
    setBusy(true);
    setStatus("");
    setSelected(null);
    try {
      const res = await apiFetch(`/api/records?patient_id=${encodeURIComponent(requestedId)}`);
      const json = await res.json();
      if (patientIdRef.current !== requestedId) return; // patient changed while this was in flight
      const encounters: EncounterBlock[] = json.encounters ?? [];
      setData(encounters);
      setSelected(encounters[0] ?? null);
      if (encounters.length === 0) setStatus("No records found for this patient.");
    } catch (e) {
      if (patientIdRef.current === requestedId) {
        setStatus(e instanceof Error ? e.message : "Could not load records.");
        setData([]);
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="rb-stack">
      <div className="rb-page-head">
        <h1>Health Records</h1>
        <p>Look up a patient&apos;s encounters and lab results.</p>
      </div>

      <Card>
        <div className="rb-field" style={{ maxWidth: 360, marginBottom: 0 }}>
          <label className="rb-field__label" htmlFor="rec-patient">
            Patient ID
          </label>
          <div style={{ display: "flex", gap: 8 }}>
            <input
              id="rec-patient"
              className="rb-input"
              value={patientId}
              onChange={(e) => handlePatientIdChange(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && load()}
              inputMode="numeric"
            />
            <button className="rb-btn rb-btn--primary" onClick={load} disabled={busy} type="button">
              {busy ? "Loading…" : <><IconSearch width={16} height={16} /> Load</>}
            </button>
          </div>
          <span className="rb-field__hint">Demo patient ID defaults to 1042.</span>
        </div>
      </Card>

      {status && (
        <div className="rb-alert rb-alert--info" role="status">
          {status}
        </div>
      )}

      <Card title="AI-Assisted Chart View" icon={<IconStethoscope />}>
        <p className="rb-muted" style={{ marginTop: 0 }}>
          A bounded, evidence-cited summary of the same patient ID above, produced by the
          Stage&nbsp;3 patient-view assistant. This checks that you are logged in as staff — it
          does not verify you are assigned to this specific patient.
        </p>
        <button className="rb-btn rb-btn--ghost" onClick={loadAiView} disabled={aiBusy} type="button">
          {aiBusy ? "Loading…" : "Generate AI chart view"}
        </button>

        {aiStatus && (
          <div className="rb-alert rb-alert--err" role="status" style={{ marginTop: 12 }}>
            {aiStatus}
          </div>
        )}

        {aiView && (
          <div style={{ marginTop: 14 }}>
            <span className="rb-eyebrow">Chart review</span>
            <div style={{ display: "flex", alignItems: "center", gap: 10, flexWrap: "wrap" }}>
              <h3 style={{ margin: 0 }}>{AI_HEADING[aiView.outcome]}</h3>
              <StatusBadge status={aiView.outcome} />
            </div>
            <p style={{ marginTop: 10 }}>{aiView.summary}</p>

            {aiView.evidence_ids.length > 0 && (
              <div style={{ marginBottom: 10 }}>
                <div className="rb-eyebrow">Evidence</div>
                <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
                  {aiView.evidence_ids.map((id) => (
                    <span key={id} className="rb-badge rb-badge--neutral">
                      {id}
                    </span>
                  ))}
                </div>
              </div>
            )}

            {aiView.limitations.length > 0 && (
              <div style={{ marginBottom: 10 }}>
                <div className="rb-eyebrow">Limitations</div>
                <ul className="rb-muted" style={{ marginTop: 0 }}>
                  {aiView.limitations.map((l) => (
                    <li key={l}>{l}</li>
                  ))}
                </ul>
              </div>
            )}

            {(aiView.outcome === "escalated" || aiView.outcome === "refused") && (
              <div
                className={`rb-alert rb-alert--${aiView.outcome === "refused" ? "err" : "warn"}`}
                role="alert"
              >
                <strong>Clinician review required.</strong> {AI_CALLOUT[aiView.outcome]}
              </div>
            )}
          </div>
        )}
      </Card>

      <Card title="Possible Duplicate Records" icon={<IconSearch />}>
        <p className="rb-muted" style={{ marginTop: 0 }}>
          Checks whether any other chart shares this patient&apos;s exact Social
          Security Number — a read-only candidate signal for a clinician to
          review, never an automatic match or merge.
        </p>
        <button
          className="rb-btn rb-btn--ghost"
          onClick={loadReconciliation}
          disabled={reconciliationBusy}
          type="button"
        >
          {reconciliationBusy ? "Checking…" : "Check for related records"}
        </button>

        {reconciliationStatus && (
          <div className="rb-alert rb-alert--err" role="status" style={{ marginTop: 12 }}>
            {reconciliationStatus}
          </div>
        )}

        {reconciliation && (
          <div style={{ marginTop: 14 }}>
            <span className="rb-eyebrow">Record review</span>
            <h3 style={{ margin: 0 }}>
              {reconciliation.source_records.length > 1
                ? "These records may describe one patient."
                : "No reconciliation candidates were returned for this chart."}
            </h3>

            {reconciliation.escalation && (
              <div className="rb-alert rb-alert--warn" role="alert" style={{ marginTop: 10 }}>
                <strong>Clinician review required.</strong> This is evidence for a
                clinician to assess — not an automatic merge, diagnosis, or
                treatment decision.
              </div>
            )}

            {reconciliation.source_records.length > 1 && (
              <div className="rb-table-scroll">
                <table className="rb-table">
                  <thead>
                    <tr>
                      <th>Chart</th>
                      <th>Name on file</th>
                      <th>Date of birth</th>
                      <th>Allergies</th>
                    </tr>
                  </thead>
                  <tbody>
                    {reconciliation.source_records.map((r) => (
                      <tr key={r.patient_id}>
                        <td>
                          {r.patient_id}
                          <div className="rb-muted" style={{ fontSize: "0.78rem" }}>
                            {r.source_label}
                          </div>
                        </td>
                        <td>{r.name_on_file}</td>
                        <td>{r.dob ?? "—"}</td>
                        <td>
                          {r.allergies.length === 0
                            ? "none recorded"
                            : r.allergies.map((a) => {
                                const flagged = reconciliation.discrepancies.some(
                                  (d) =>
                                    d.category === "allergy" &&
                                    d.value === a &&
                                    d.present_on_patient_ids.includes(r.patient_id)
                                );
                                return (
                                  <span
                                    key={a}
                                    style={{ display: "inline-flex", alignItems: "center", gap: 6, marginRight: 8 }}
                                  >
                                    {a}
                                    {flagged && (
                                      <span className="rb-badge rb-badge--warn">not on all charts</span>
                                    )}
                                  </span>
                                );
                              })}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}

            {reconciliation.discrepancies.length > 0 && (
              <ul className="rb-muted" style={{ marginTop: 10 }}>
                {reconciliation.discrepancies.map((d) => (
                  <li key={`${d.category}-${d.value}`}>
                    {d.category === "allergy" ? "Allergy" : "Medication"} &quot;{d.value}&quot; recorded on
                    chart{d.present_on_patient_ids.length > 1 ? "s" : ""}{" "}
                    {d.present_on_patient_ids.join(", ")}, not on{" "}
                    {d.missing_on_patient_ids.join(", ")}.
                  </li>
                ))}
              </ul>
            )}

            {reconciliation.identity_signals.length > 0 && (
              <p className="rb-muted" style={{ marginTop: 10 }}>
                Matching signal: {reconciliation.identity_signals.map((s) => s.masked_value).join(", ")}
              </p>
            )}

            {reconciliation.discrepancies.length > 0 && (
              <div style={{ marginTop: 10 }}>
                <div className="rb-eyebrow">Evidence</div>
                <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
                  {Array.from(new Set(reconciliation.discrepancies.flatMap((d) => d.evidence_ids))).map((id) => (
                    <span key={id} className="rb-badge rb-badge--neutral">
                      {id}
                    </span>
                  ))}
                </div>
              </div>
            )}

            {reconciliation.limitations.length > 0 && (
              <div style={{ marginTop: 10 }}>
                <div className="rb-eyebrow">Limitations</div>
                <ul className="rb-muted" style={{ marginTop: 0 }}>
                  {reconciliation.limitations.map((l) => (
                    <li key={l}>{l}</li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        )}
      </Card>

      {data && data.length > 0 && (
        <div className="rb-grid rb-grid--2">
          {/* Encounter list */}
          <Card title="Encounters" icon={<IconRecords />}>
            <div className="rb-list">
              {data.map((block) => {
                const active = selected?.encounter.id === block.encounter.id;
                return (
                  <button
                    key={block.encounter.id}
                    type="button"
                    className="rb-listrow rb-listrow--clickable"
                    aria-pressed={active}
                    style={active ? { borderColor: "var(--rb-primary)" } : undefined}
                    onClick={() => setSelected(block)}
                  >
                    <div className="rb-listrow__main">
                      <div className="rb-listrow__title">{block.encounter.type}</div>
                      <div className="rb-listrow__meta">
                        <span><IconStethoscope width={15} height={15} /> {block.encounter.provider}</span>
                        {block.encounter.date && <span>{fmtDate(block.encounter.date)}</span>}
                        <span>{block.records.length} record{block.records.length === 1 ? "" : "s"}</span>
                      </div>
                    </div>
                  </button>
                );
              })}
            </div>
          </Card>

          {/* Encounter detail */}
          <Card
            title={selected ? selected.encounter.type : "Encounter detail"}
            icon={<IconLab />}
          >
            {selected ? (
              <EncounterDetail block={selected} />
            ) : (
              <div className="rb-empty">Select an encounter to view details.</div>
            )}
          </Card>
        </div>
      )}
    </div>
  );
}

function EncounterDetail({ block }: { block: EncounterBlock }) {
  const results = block.records.filter(isResult);
  const notes = block.records.filter((r) => !isResult(r));

  return (
    <div>
      <div className="rb-listrow__meta" style={{ marginBottom: 6 }}>
        <span><IconStethoscope width={15} height={15} /> {block.encounter.provider}</span>
        {block.encounter.date && <span>{fmtDate(block.encounter.date)}</span>}
      </div>
      {block.encounter.summary && (
        <p className="rb-muted">{block.encounter.summary}</p>
      )}

      {results.length > 0 && (
        <>
          <h3 style={{ marginTop: 18 }}>Lab results</h3>
          <table className="rb-table">
            <thead>
              <tr>
                <th>Test</th>
                <th>Value</th>
                <th>Reference range</th>
                <th>Status</th>
              </tr>
            </thead>
            <tbody>
              {results.map((r) => {
                const abnormal = statusVariant(r.status) === "bad";
                return (
                  <tr key={r.id}>
                    <td>{r.test || r.kind}</td>
                    <td className={`rb-table__num${abnormal ? " rb-table__num--abnormal" : ""}`}>
                      {r.value ?? "—"}
                      {r.unit ? ` ${r.unit}` : ""}
                    </td>
                    <td className="rb-ref">{r.reference_range ?? "—"}</td>
                    <td><StatusBadge status={r.status || "normal"} /></td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </>
      )}

      {notes.length > 0 && (
        <>
          <h3 style={{ marginTop: 18 }}>Records &amp; notes</h3>
          <div className="rb-list">
            {notes.map((r) => (
              <div key={r.id} className="rb-listrow" style={{ display: "block" }}>
                <span className="rb-badge rb-badge--neutral" style={{ marginBottom: 6 }}>
                  {r.kind}
                </span>
                <div>{r.body}</div>
              </div>
            ))}
          </div>
        </>
      )}

      {results.length === 0 && notes.length === 0 && (
        <div className="rb-empty">No records in this encounter.</div>
      )}
    </div>
  );
}
