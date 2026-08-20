"use client";

import { useEffect, useRef, useState } from "react";
import { apiFetch } from "../lib/session";

// The patient's name, in a display-only box between the Patient ID field and
// the Load button on the records and appointments screens (client request,
// 2026-08-16; boxed and reordered after review, 2026-08-20).
//
// It holds its own space whether or not a name is resolved, so the row does not
// reflow when one arrives — a name appearing mid-row used to push the Load
// button sideways under the cursor.
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
  // box it reads as a broken screen, so it says so instead. The waiting state
  // is faint placeholder text rather than blank, for the same reason.
  const placeholder = unavailable ? "Name unavailable" : "Patient name";
  const resolved = Boolean(name);

  return (
    <div
      className={`rb-input rb-input--readonly${resolved ? "" : " rb-input--placeholder"}`}
      style={{ flex: "0 1 220px" }}
      aria-label="Patient name"
      title={name ?? placeholder}
      data-testid="patient-name"
    >
      {name ?? placeholder}
    </div>
  );
}
