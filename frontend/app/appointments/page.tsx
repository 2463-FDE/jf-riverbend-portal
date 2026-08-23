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

  // Round-1 review (M2): tracks the most recently REQUESTED id, independent
  // of loadedPatientId — which is now set only on a SUCCESSFUL load (see
  // below), so it can no longer double as "is this response still current."
  const latestRequestedIdRef = useRef("");

  // Takes the id explicitly rather than reading patientId off state, so a
  // post-booking refresh can target the id that was actually just booked for
  // (loadedPatientId) without racing whatever the input currently holds.
  const loadAppts = useCallback(async (id: string) => {
    if (!isValidPatientId(id)) return;
    latestRequestedIdRef.current = id;
    setAppts(null);
    setApptsBusy(true);
    try {
      const r = await apiFetch(`/api/appointments?patient_id=${encodeURIComponent(id)}`);
      if (latestRequestedIdRef.current !== id) return; // superseded by a later Load
      if (!r.ok) {
        // Round-1 review (M2): a denied or failed load has no `items`, so
        // treating it like a real response rendered "No appointments for
        // this patient yet." — indistinguishable from a patient who
        // genuinely has none — and left Book enabled for a patient that was
        // never actually confirmed. loadedPatientId is left unset here
        // (never set eagerly by handleLoad any more), so Book stays
        // disabled and the banner carries the real reason instead.
        setMsg({
          kind: "err",
          text:
            r.status === 403
              ? "You are not authorized to view this patient's appointments."
              : "Could not load appointments for this patient.",
        });
        return;
      }
      const d = await r.json();
      if (latestRequestedIdRef.current !== id) return;
      setLoadedPatientId(id);
      setAppts(Array.isArray(d) ? d : (d.items ?? []));
    } catch {
      if (latestRequestedIdRef.current === id) {
        setMsg({ kind: "err", text: "Could not load appointments for this patient." });
      }
    } finally {
      if (latestRequestedIdRef.current === id) setApptsBusy(false);
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
    // w9-fixes P1 4.1: changing the patient must drop every trace of the
    // PREVIOUS patient's transient state, not just their name/list — a
    // leftover error/success banner, a typed reason, a pending
    // book/cancel spinner, or a reused idempotency key could otherwise be
    // misread as belonging to whoever is loaded next.
    setLoadedPatientId("");
    latestRequestedIdRef.current = "";
    setAppts(null);
    // Round-2 review (M3): invalidating latestRequestedIdRef above means the
    // abandoned request's own `finally` will refuse to clear apptsBusy
    // (its `latestRequestedIdRef.current === id` check now fails), so
    // without this the Load button — disabled while apptsBusy is true —
    // stayed disabled forever after editing the id mid-load.
    setApptsBusy(false);
    setMsg(null);
    setReason("");
    setBusySlot(null);
    setBusyCancel(null);
    idempotencyKeysRef.current.clear();
  }

  function handleLoad() {
    if (!isValidPatientId(patientId)) return;
    // Cleared here rather than inside loadAppts itself — book()/cancel()
    // also call loadAppts, as a post-mutation refresh, and must not have
    // their own just-set success banner wiped by that internal call.
    setMsg(null);
    // Round-1 review (M2): loadedPatientId is now set by loadAppts itself,
    // only once that call actually succeeds — setting it here unconditionally
    // is exactly what let a denied/failed load look identical to "loaded, zero
    // appointments" and left Book enabled for a patient that was never
    // confirmed.
    loadAppts(patientId);
  }

  async function book(slot: Slot) {
    // w9-fixes P1 4.1: bind the mutation to the CONFIRMED patient, not
    // whatever is currently typed — the id field can be edited again while
    // a booking is in flight, and the raw patientId no longer reflects who
    // this request is actually for.
    const patientForRequest = loadedPatientId;
    if (!isValidPatientId(patientForRequest)) return;
    setBusySlot(slot.id);
    setMsg(null);
    try {
      const r = await apiFetch("/api/appointments", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          patient_id: Number(patientForRequest),
          slot_id: slot.id,
          idempotency_key: idempotencyKeyFor(slot.id),
          // w9-fixes P0 4.2/4.3: provider/location/scheduled_for are no
          // longer sent — the backend now derives all three from the
          // slot_id itself and ignores anything the caller sends for them,
          // so sending slot.provider here was already inert.
          reason: reason || "Office visit",
        }),
      });
      if (!r.ok) throw new Error();
      idempotencyKeysRef.current.delete(slot.id);
      // A slow booking whose patient was since changed must not paint a
      // stale success (or refresh the wrong id's list) over whoever is
      // loaded now.
      if (loadedPatientIdRef.current !== patientForRequest) return;
      setMsg({ kind: "ok", text: `Appointment booked with ${slot.provider}.` });
      setReason("");
      await Promise.all([loadAppts(patientForRequest), loadSlots()]);
    } catch {
      if (loadedPatientIdRef.current !== patientForRequest) return;
      setMsg({ kind: "err", text: "Could not book that slot. Please try another." });
    } finally {
      if (loadedPatientIdRef.current === patientForRequest) setBusySlot(null);
    }
  }

  async function cancel(appt: Appointment) {
    const patientForRequest = loadedPatientId;
    setBusyCancel(appt.id);
    setMsg(null);
    try {
      const r = await apiFetch(`/api/appointments/${appt.id}/cancel`, { method: "POST" });
      if (!r.ok) throw new Error();
      if (loadedPatientIdRef.current !== patientForRequest) return;
      setMsg({ kind: "ok", text: "Appointment cancelled." });
      await loadAppts(patientForRequest);
    } catch {
      if (loadedPatientIdRef.current !== patientForRequest) return;
      setMsg({ kind: "err", text: "Could not cancel that appointment." });
    } finally {
      if (loadedPatientIdRef.current === patientForRequest) setBusyCancel(null);
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
                          <span><IconClock width={15} height={15} /> {fmtDateTime(a.scheduled_for)}</span>
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
                    disabled={busySlot === s.id || !isValidPatientId(loadedPatientId)}
                    title={isValidPatientId(loadedPatientId) ? undefined : "Load a patient first"}
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
