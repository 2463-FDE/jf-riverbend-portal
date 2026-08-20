"use client";

import { useEffect, useRef, useState } from "react";
import { apiFetch } from "../lib/session";

// The patient's name, shown beside the bare Patient ID on the records and
// appointments screens (client request, 2026-08-16).
//
// `patientId` must be the LOADED id, not the input's live value. Every
// successful get_patient writes an audit row (records-service _write_audit),
// so a name driven by the input would write one audit row per character typed.
// Both callers pass an id that only changes when Load is pressed, and pass ""
// to clear.
//
// The stale-response guard is here rather than borrowed from the page: the
// records screen has one (patientIdRef) but the appointments screen has none,
// and a name that lags one patient behind the chart beneath it is worse than
// no name at all.
export default function PatientName({ patientId }: { patientId: string }) {
  const [name, setName] = useState<string | null>(null);
  const [unavailable, setUnavailable] = useState(false);
  const requestedRef = useRef(patientId);

  useEffect(() => {
    requestedRef.current = patientId;
    setName(null);
    setUnavailable(false);
    if (!patientId) return;

    (async () => {
      try {
        const res = await apiFetch(`/api/patients/${encodeURIComponent(patientId)}/name`);
        const json = await res.json().catch(() => ({}));
        if (requestedRef.current !== patientId) return; // id moved on while in flight
        if (!res.ok || !json?.name) {
          setUnavailable(true);
          return;
        }
        setName(json.name as string);
      } catch {
        if (requestedRef.current === patientId) setUnavailable(true);
      }
    })();
  }, [patientId]);

  // A denied lookup is the authorization boundary working correctly — grants
  // are sparse, so e.g. front desk holds 1042 but not 1737. Left as an empty
  // gap it reads as a broken screen, so it says so instead.
  if (unavailable) {
    return (
      <span className="rb-muted" style={{ fontSize: 13, whiteSpace: "nowrap" }}>
        Name unavailable
      </span>
    );
  }
  if (!name) return null;

  return (
    <span style={{ fontWeight: 600, whiteSpace: "nowrap" }} data-testid="patient-name">
      {name}
    </span>
  );
}
