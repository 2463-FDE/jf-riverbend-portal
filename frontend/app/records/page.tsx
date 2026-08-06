"use client";

import { useState } from "react";
import Card from "../components/Card";
import StatusBadge, { statusVariant } from "../components/StatusBadge";
import { IconRecords, IconLab, IconSearch, IconStethoscope } from "../components/icons";
import { apiFetch } from "../lib/session";
import type { EncounterBlock, PatientViewResult, RecordItem } from "../lib/types";
import { fmtDate } from "../lib/format";

function isResult(r: RecordItem): boolean {
  return Boolean(r.test || r.value !== undefined || r.reference_range);
}

function aiOutcomeBadgeClass(outcome: PatientViewResult["outcome"]): string {
  if (outcome === "completed") return "rb-badge--ok";
  if (outcome === "escalated") return "rb-badge--warn";
  return "rb-badge--bad";
}

export default function RecordsPage() {
  // The records view loads by a patient id taken straight off the input/URL.
  // The id is a sequential integer and the backend does NOT check ownership
  // (IDOR — intentional teaching point; see docs/handover/portal.har). We pass
  // whatever id is entered straight through to /api/records.
  const [patientId, setPatientId] = useState("1042");
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

  async function loadAiView() {
    setAiBusy(true);
    setAiStatus("");
    setAiView(null);
    try {
      const res = await apiFetch(`/api/patients/${encodeURIComponent(patientId)}/view`);
      const json = await res.json();
      if (!res.ok) {
        const detail = json?.detail;
        const reason = typeof detail === "string" ? detail : detail?.reason;
        setAiStatus(reason ? `Could not load AI chart view: ${reason}` : "Could not load AI chart view.");
        return;
      }
      setAiView(json as PatientViewResult);
    } catch (e) {
      setAiStatus(e instanceof Error ? e.message : "Could not load AI chart view.");
    } finally {
      setAiBusy(false);
    }
  }

  async function load() {
    setBusy(true);
    setStatus("");
    setSelected(null);
    try {
      const res = await apiFetch(`/api/records?patient_id=${encodeURIComponent(patientId)}`);
      const json = await res.json();
      const encounters: EncounterBlock[] = json.encounters ?? [];
      setData(encounters);
      setSelected(encounters[0] ?? null);
      if (encounters.length === 0) setStatus("No records found for this patient.");
    } catch (e) {
      setStatus(e instanceof Error ? e.message : "Could not load records.");
      setData([]);
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
              onChange={(e) => setPatientId(e.target.value)}
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
            <span className={`rb-badge ${aiOutcomeBadgeClass(aiView.outcome)}`}>{aiView.outcome}</span>
            <p style={{ marginTop: 10 }}>{aiView.summary}</p>
            {aiView.evidence_ids.length > 0 && (
              <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginBottom: 10 }}>
                {aiView.evidence_ids.map((id) => (
                  <span key={id} className="rb-badge rb-badge--neutral">
                    {id}
                  </span>
                ))}
              </div>
            )}
            {aiView.limitations.length > 0 && (
              <ul className="rb-muted" style={{ marginTop: 0 }}>
                {aiView.limitations.map((l) => (
                  <li key={l}>{l}</li>
                ))}
              </ul>
            )}
            {aiView.escalation && (
              <div className="rb-alert rb-alert--info" role="status">
                This view requires human review before use.
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
