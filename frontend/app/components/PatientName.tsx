"use client";

import { identityLine, usePatientIdentity } from "../lib/usePatientIdentity";

// The patient's identity, in a display-only box between the Patient ID field
// and the Load button on the records and appointments screens (client
// request, 2026-08-16; boxed and reordered after review, 2026-08-20; format
// standardized to "{Name} — Patient ID {id}" 2026-08-22, then narrowed on
// 2026-08-22 — see `nameOnly` below).
//
// `nameOnly` (records and appointments ONLY): those two screens already show
// the id in the adjacent Patient ID input, so repeating "Patient ID {id}"
// inside this box too was redundant with something already on screen one
// field over. Every OTHER caller (the review queue's per-case label, the
// agent-draft panel, a patient's own identity on /my-results and the
// approved agent-summary) has no adjacent id anywhere and keeps the combined
// "{Name} — Patient ID {id}" string — those are the "standalone" contexts
// this box is the ONLY source of the id for. Default is combined, so a
// caller that forgets the prop gets the safer, more self-contained behavior.
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
export default function PatientName({
  patientId,
  nameOnly = false,
}: {
  patientId: string;
  nameOnly?: boolean;
}) {
  const path = patientId ? `/api/patients/${encodeURIComponent(patientId)}/name` : null;
  const { name, unavailable } = usePatientIdentity(path, patientId);

  // A denied lookup is the authorization boundary working correctly — grants
  // are sparse, so e.g. front desk holds 1042 but not 1737. Left as an empty
  // box it reads as a broken screen, so it says so instead. The waiting state
  // is faint placeholder text rather than blank, for the same reason.
  const placeholder = unavailable ? "Name unavailable" : "Patient name";
  // Combined, never the name alone: "consistent identity" means the same
  // two-fact string everywhere a patient is identified, not a name that reads
  // differently depending which screen happened to render it. Unresolved or
  // unauthorized states are UNCHANGED from before — no identity information at
  // all, not even the id, is added to what this box shows when there is
  // nothing this viewer may see.
  const display = (nameOnly ? name : identityLine(name, patientId)) ?? placeholder;
  const resolved = Boolean(name);

  return (
    <div
      className={`rb-input rb-input--readonly${resolved ? "" : " rb-input--placeholder"}`}
      style={{ flex: "0 1 260px" }}
      // NOT "Patient identity" — that string contains "Patient id" as a
      // literal substring ("id" being the start of "identity"), which collides
      // with getByLabelText(/patient id/i) queries against the real Patient ID
      // input on the same screen and made two elements match one query.
      aria-label="Resolved patient name"
      title={display}
      data-testid="patient-name"
    >
      {display}
    </div>
  );
}
