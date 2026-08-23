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
import PatientName from "../components/PatientName";
import { apiFetch } from "../lib/session";
import type { Appointment, Slot } from "../lib/types";
import { fmtDateTime, fmtTimeRange, fmtDate } from "../lib/format";

export default function AppointmentsPage() {
  // Blank on first render (2026-08-23, W9.0) — same reasoning as
  // records/page.tsx: a hardcoded default id made a patient-specific request
  // before any staff member had actually chosen a patient. Booking still
  // reads this raw value directly (see book() below); only the list fetch
  // and the resolved name require a completed Load.
  const [patientId, setPatientId] = useState("");
  const isValidPatientId = (id: string) => /^\d+$/.test(id.trim());

  // The id whose name is shown beside the input and whose list was actually
  // fetched — set only by Load (or Enter), never by typing. Mirrored into a
  // ref so an in-flight loadAppts response can tell whether it is still the
  // most recently requested id before applying it (a slow response for an id
  // the staff member already moved on from must not land).
  const [loadedPatientId, setLoadedPatientId] = useState("");
  const loadedPatientIdRef = useRef(loadedPatientId);
  loadedPatientIdRef.current = loadedPatientId;

  const [appts, setAppts] = useState<Appointment[] | null>(null);
  const [apptsBusy, setApptsBusy] = useState(false);
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

  // Takes the id explicitly rather than reading patientId off state, so a
  // post-booking refresh can target the id that was actually just booked for
  // (loadedPatientId) without racing whatever the input currently holds.
  const loadAppts = useCallback(async (id: string) => {
    if (!isValidPatientId(id)) return;
    setAppts(null);
    setApptsBusy(true);
    try {
      const r = await apiFetch(`/api/appointments?patient_id=${encodeURIComponent(id)}`);
      const d = await r.json();
      if (loadedPatientIdRef.current !== id) return; // superseded by a later Load
      setAppts(Array.isArray(d) ? d : (d.items ?? []));
    } catch {
      if (loadedPatientIdRef.current === id) setAppts([]);
    } finally {
      if (loadedPatientIdRef.current === id) setApptsBusy(false);
    }
  }, []);

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

  // Open slots are not patient-specific, so this is the only auto-fetch on
  // mount. The appointment list itself waits for Load (see loadAppts above).
  useEffect(() => {
    loadSlots();
  }, [loadSlots]);

  function handlePatientIdChange(value: string) {
    setPatientId(value);
    // Drop the previous patient's name and list immediately, whether the id
    // was edited or cleared entirely — a name or an appointment left behind
    // would sit above the wrong (or no) patient.
    setLoadedPatientId("");
    setAppts(null);
  }

  function handleLoad() {
    if (!isValidPatientId(patientId)) return;
    setLoadedPatientId(patientId);
    loadAppts(patientId);
  }

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
      // A successful booking confirms this id the same way pressing Load
      // would, so the list staff just added to reflects it immediately.
      setLoadedPatientId(patientId);
      await Promise.all([loadAppts(patientId), loadSlots()]);
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
      await loadAppts(loadedPatientId);
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
        <div className="rb-field" style={{ marginBottom: 0 }}>
          <label className="rb-field__label" htmlFor="appt-patient">
            Patient ID
          </label>
          <div style={{ display: "flex", gap: 8, alignItems: "center", maxWidth: 520 }}>
            <input
              id="appt-patient"
              className="rb-input"
              style={{ flex: "0 1 160px" }}
              placeholder="Patient ID"
              value={patientId}
              onChange={(e) => handlePatientIdChange(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && handleLoad()}
              inputMode="numeric"
            />
            {/* nameOnly (2026-08-22): same reasoning as records/page.tsx —
                the id is already shown in the adjacent Patient ID input. */}
            <PatientName patientId={loadedPatientId} nameOnly />
            <button className="rb-btn" onClick={handleLoad} disabled={apptsBusy} type="button">
              Load
            </button>
          </div>
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
            apptsBusy ? (
              <Loading label="Loading appointments…" />
            ) : (
              <div className="rb-empty">Enter a Patient ID above and press Load to see appointments.</div>
            )
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
