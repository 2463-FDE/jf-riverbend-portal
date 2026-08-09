"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Card from "../components/Card";
import StatusBadge from "../components/StatusBadge";
import {
  IconCalendar,
  IconClock,
  IconPin,
  IconStethoscope,
  IconPlus,
} from "../components/icons";
import EligibilityChat from "../components/EligibilityChat";
import { apiFetch } from "../lib/session";
import type { Appointment, Slot } from "../lib/types";
import { fmtDateTime, fmtTimeRange, fmtDate } from "../lib/format";

const DEFAULT_PATIENT_ID = "1042";

export default function AppointmentsPage() {
  const [patientId, setPatientId] = useState(DEFAULT_PATIENT_ID);
  const [appts, setAppts] = useState<Appointment[] | null>(null);
  const [slots, setSlots] = useState<Slot[] | null>(null);
  const [reason, setReason] = useState("");
  const [busySlot, setBusySlot] = useState<number | null>(null);
  const [busyCancel, setBusyCancel] = useState<number | null>(null);
  const [msg, setMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(null);

  // Stage 4 (Week 5, RIV-175): one idempotency key per slot per booking
  // attempt, not a fresh one on every call. If the same slot's "Book" button
  // gets clicked again before we know the first attempt's outcome (a slow
  // request, a client timeout, an accidental double-click), this reuses the
  // SAME key so the backend's replay handling (book.py) returns the original
  // confirmation instead of racing a second booking for the same slot. A
  // fresh key is generated only the first time a given slot is booked.
  const idempotencyKeysRef = useRef<Map<number, string>>(new Map());

  function idempotencyKeyFor(slotId: number): string {
    const existing = idempotencyKeysRef.current.get(slotId);
    if (existing) return existing;
    const key = crypto.randomUUID();
    idempotencyKeysRef.current.set(slotId, key);
    return key;
  }

  const loadAppts = useCallback(async () => {
    setAppts(null);
    try {
      const r = await apiFetch(`/api/appointments?patient_id=${encodeURIComponent(patientId)}`);
      const d = await r.json();
      setAppts(Array.isArray(d) ? d : (d.items ?? []));
    } catch {
      setAppts([]);
    }
  }, [patientId]);

  const loadSlots = useCallback(async () => {
    setSlots(null);
    try {
      const r = await apiFetch(`/api/slots?limit=12`);
      const d = await r.json();
      setSlots(d.items ?? []);
    } catch {
      setSlots([]);
    }
  }, []);

  useEffect(() => {
    loadAppts();
    loadSlots();
  }, [loadAppts, loadSlots]);

  async function book(slot: Slot) {
    setBusySlot(slot.id);
    setMsg(null);
    try {
      const r = await apiFetch("/api/appointments", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          patient_id: Number(patientId) || patientId,
          slot_id: slot.id,
          idempotency_key: idempotencyKeyFor(slot.id),
          provider: slot.provider,
          reason: reason || "Office visit",
        }),
      });
      if (!r.ok) throw new Error();
      idempotencyKeysRef.current.delete(slot.id);
      setMsg({ kind: "ok", text: `Appointment booked with ${slot.provider}.` });
      setReason("");
      await Promise.all([loadAppts(), loadSlots()]);
    } catch {
      setMsg({ kind: "err", text: "Could not book that slot. Please try another." });
    } finally {
      setBusySlot(null);
    }
  }

  async function cancel(appt: Appointment) {
    setBusyCancel(appt.id);
    setMsg(null);
    try {
      const r = await apiFetch(`/api/appointments/${appt.id}/cancel`, { method: "POST" });
      if (!r.ok) throw new Error();
      setMsg({ kind: "ok", text: "Appointment cancelled." });
      await loadAppts();
    } catch {
      setMsg({ kind: "err", text: "Could not cancel that appointment." });
    } finally {
      setBusyCancel(null);
    }
  }

  const openSlots = (slots ?? []).filter(
    (s) => !["booked", "cancelled", "canceled", "unavailable"].includes(s.status?.toLowerCase())
  );

  return (
    <div className="rb-stack">
      <div className="rb-page-head">
        <h1>Appointments</h1>
        <p>Review your upcoming visits and schedule a new one.</p>
      </div>

      <Card>
        <div className="rb-field" style={{ maxWidth: 280, marginBottom: 0 }}>
          <label className="rb-field__label" htmlFor="appt-patient">
            Patient ID
          </label>
          <div style={{ display: "flex", gap: 8 }}>
            <input
              id="appt-patient"
              className="rb-input"
              value={patientId}
              onChange={(e) => setPatientId(e.target.value)}
            />
            <button className="rb-btn" onClick={loadAppts} type="button">
              Load
            </button>
          </div>
          <span className="rb-field__hint">Demo patient ID defaults to 1042.</span>
        </div>
      </Card>

      {msg && (
        <div className={`rb-alert rb-alert--${msg.kind === "ok" ? "ok" : "err"}`} role="status">
          {msg.text}
        </div>
      )}

      <div className="rb-grid rb-grid--2">
        <Card title="Your appointments" icon={<IconCalendar />}>
          {appts === null ? (
            <Loading label="Loading appointments…" />
          ) : appts.length ? (
            <div className="rb-list">
              {appts.map((a) => {
                const cancelled = ["cancelled", "canceled"].includes(a.status?.toLowerCase());
                return (
                  <div className="rb-listrow" key={a.id} style={{ flexDirection: "column", alignItems: "stretch" }}>
                    <div style={{ display: "flex", justifyContent: "space-between", gap: 12 }}>
                      <div className="rb-listrow__main">
                        <div className="rb-listrow__title">{a.reason || "Office visit"}</div>
                        <div className="rb-listrow__meta">
                          <span><IconStethoscope width={15} height={15} /> {a.provider}</span>
                          <span><IconClock width={15} height={15} /> {fmtDateTime(a.start_at)}</span>
                          {a.location && <span><IconPin width={15} height={15} /> {a.location}</span>}
                        </div>
                      </div>
                      <div style={{ display: "flex", flexDirection: "column", gap: 8, alignItems: "flex-end" }}>
                        <StatusBadge status={a.status} />
                        {!cancelled && (
                          <button
                            className="rb-btn rb-btn--danger rb-btn--sm"
                            onClick={() => cancel(a)}
                            disabled={busyCancel === a.id}
                            type="button"
                          >
                            {busyCancel === a.id ? "Cancelling…" : "Cancel"}
                          </button>
                        )}
                      </div>
                    </div>
                    {/* Stage 2 (feature-readiness): eligibility chat, scoped
                        to this appointment. The gateway derives which
                        patient/insurance this chat can discuss from the
                        appointment id itself — see EligibilityChat.tsx. */}
                    {!cancelled && <EligibilityChat appointmentId={a.id} />}
                  </div>
                );
              })}
            </div>
          ) : (
            <div className="rb-empty">No appointments for this patient yet.</div>
          )}
        </Card>

        <Card title="Schedule a visit" icon={<IconPlus />}>
          <div className="rb-field">
            <label className="rb-field__label" htmlFor="appt-reason">
              Reason for visit
            </label>
            <input
              id="appt-reason"
              className="rb-input"
              placeholder=""
              value={reason}
              onChange={(e) => setReason(e.target.value)}
            />
            <span className="rb-field__hint">
              Optional — defaults to &quot;Office visit&quot;. Pick an open slot below to book.
            </span>
          </div>

          <hr className="rb-divider" />

          {slots === null ? (
            <Loading label="Finding open slots…" />
          ) : openSlots.length ? (
            <div className="rb-list">
              {openSlots.map((s) => (
                <div className="rb-listrow" key={s.id}>
                  <div className="rb-listrow__main">
                    <div className="rb-listrow__title">{s.provider}</div>
                    <div className="rb-listrow__meta">
                      <span><IconCalendar width={15} height={15} /> {fmtDate(s.start_at)}</span>
                      <span><IconClock width={15} height={15} /> {fmtTimeRange(s.start_at, s.end_at)}</span>
                      {s.location && <span><IconPin width={15} height={15} /> {s.location}</span>}
                    </div>
                  </div>
                  <button
                    className="rb-btn rb-btn--primary rb-btn--sm"
                    onClick={() => book(s)}
                    disabled={busySlot === s.id}
                    type="button"
                  >
                    {busySlot === s.id ? "Booking…" : "Book"}
                  </button>
                </div>
              ))}
            </div>
          ) : (
            <div className="rb-empty">No open slots available right now.</div>
          )}
        </Card>
      </div>
    </div>
  );
}

function Loading({ label }: { label: string }) {
  return (
    <div className="rb-loading">
      <span className="rb-spinner rb-spinner--dark" aria-hidden="true" /> {label}
    </div>
  );
}
