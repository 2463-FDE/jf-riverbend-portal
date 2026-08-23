"use client";

import { useEffect, useRef, useState } from "react";
import { apiFetch } from "./session";

// The single fetch-and-guard behind every "{Name} — Patient ID {id}" identity
// display in the portal (2026-08-22): PatientName's boxed staff-screen widget,
// the review queue, the agent-draft panel, and the patient's own results and
// summary screens all need the same three properties — no fetch on a blank
// id, no name shown for an id that has moved on, and no leaked distinction
// between "not found" and "not authorized" — so this is the one place that
// logic is written, not four copies of it drifting apart.
//
// `path` lets a caller point at either the staff lookup (`/api/patients/{id}
// /name`, by explicit id) or a patient's own identity (`/api/patient/
// identity`, no id in the URL at all — the id below is only used to key the
// stale-response guard, not to build the request).
export function usePatientIdentity(path: string | null, key: string) {
  const [name, setName] = useState<string | null>(null);
  // The "self" identity route (/api/patient/identity) returns its own
  // patient_id because the caller does not know it in advance — a by-id
  // lookup already has the id it asked with, so this is only ever read by
  // the "self" caller (my-results).
  const [patientId, setPatientId] = useState<number | null>(null);
  const [unavailable, setUnavailable] = useState(false);
  const requestedRef = useRef(key);

  useEffect(() => {
    requestedRef.current = key;
    setName(null);
    setPatientId(null);
    setUnavailable(false);
    if (!path) return;

    (async () => {
      try {
        const res = await apiFetch(path);
        const json = await res.json().catch(() => ({}));
        if (requestedRef.current !== key) return; // moved on while in flight
        if (!res.ok || !json?.name) {
          setUnavailable(true);
          return;
        }
        setName(json.name as string);
        if (typeof json.patient_id === "number") setPatientId(json.patient_id);
      } catch {
        if (requestedRef.current === key) setUnavailable(true);
      }
    })();
  }, [path, key]);

  return { name, patientId, unavailable };
}

// The combined string every screen shows, or null when there is nothing this
// viewer may see (an unresolved or unauthorized lookup discloses no identity
// data at all — not even the id — beyond what the caller already had).
export function identityLine(name: string | null, patientId: number | string): string | null {
  return name ? `${name} — Patient ID ${patientId}` : null;
}
