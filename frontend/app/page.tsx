"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import Card from "./components/Card";
import StatusBadge from "./components/StatusBadge";
import {
  IconCalendar,
  IconLab,
  IconIntake,
  IconRecords,
  IconRoi,
  IconClock,
  IconPin,
  IconStethoscope,
  IconHeart,
} from "./components/icons";
import PatientHome from "./components/PatientHome";
import { getUser } from "./lib/session";
import type { PortalUser } from "./lib/types";
import type { Appointment, EncounterBlock, RecordItem } from "./lib/types";
import { fmtDateTime, firstName } from "./lib/format";

function isResult(r: RecordItem): boolean {
  return Boolean(r.test || r.value !== undefined || r.reference_range);
}

// The landing route for whoever is signed in. A patient session must never
// see the staff dashboard below — it asks for nothing (no per-account data
// model) and its quick actions are staff routes a patient account is refused
// outright (W9.1, 2026-08-23; see PatientHome's own docstring for why).
//
// sessionStorage is only readable client-side, so `user` starts null on
// both the server render and the first client render (same output, no
// hydration mismatch) and is only populated after mount — exactly the
// pattern AppShell already uses for its own nav. Branching on `getUser()`
// directly during render would read a value the server pass never had,
// which is the mismatch this guards against.
export default function DashboardPage() {
  const [user, setUser] = useState<PortalUser | null | undefined>(undefined);

  useEffect(() => {
    setUser(getUser());
  }, []);

  if (user === undefined) return null;
  if (user?.role === "patient") return <PatientHome />;
  return <StaffDashboard />;
}

function StaffDashboard() {
  const [appts, setAppts] = useState<Appointment[] | null>(null);
  const [results, setResults] = useState<RecordItem[] | null>(null);
  const [name, setName] = useState("there");

  // No hardcoded patient id here (removed 2026-08-22): this dashboard is the
  // landing page for WHOEVER is signed in, staff or patient, and a fixed demo
  // id previously fetched one specific patient's real appointments/results on
  // every load regardless of who was looking — for a staff account granted
  // that chart, someone else's real data appeared unlabeled as if it were a
  // generic preview; for anyone else it silently 403'd. There is no
  // account-agnostic "my appointments"/"my results" summary endpoint yet, so
  // this section shows its own empty state and links to the real screens
  // instead of guessing at a patient id.
  useEffect(() => {
    const u = getUser();
    if (u?.full_name) setName(firstName(u.full_name));
    setAppts([]);
    setResults([]);
  }, []);

  const upcoming = (appts ?? [])
    .filter((a) => !["cancelled", "canceled", "completed"].includes(a.status?.toLowerCase()))
    .sort((a, b) => (a.scheduled_for ?? "").localeCompare(b.scheduled_for ?? ""));
  const next = upcoming[0];

  return (
    <div className="rb-stack">
      <div className="rb-page-head">
        <h1>Good day, {name}</h1>
        <p>Here&apos;s a summary of your care at Riverbend Community Health.</p>
      </div>

      <div className="rb-grid rb-grid--2">
        {/* Next appointment */}
        <Card title="Next appointment" icon={<IconCalendar />}
          action={<Link href="/appointments">View all</Link>}>
          {appts === null ? (
            <Loading label="Loading appointments…" />
          ) : next ? (
            <div>
              <div className="rb-listrow__title" style={{ fontSize: "1.05rem" }}>
                {next.reason || "Office visit"}
              </div>
              <div className="rb-listrow__meta" style={{ marginTop: 6 }}>
                <span><IconStethoscope width={15} height={15} /> {next.provider}</span>
                <span><IconClock width={15} height={15} /> {fmtDateTime(next.scheduled_for)}</span>
                {next.location && <span><IconPin width={15} height={15} /> {next.location}</span>}
              </div>
              <div style={{ marginTop: 12 }}>
                <StatusBadge status={next.status} />
              </div>
            </div>
          ) : (
            <div className="rb-empty">
              No upcoming appointments.
              <div style={{ marginTop: 10 }}>
                <Link className="rb-btn rb-btn--primary rb-btn--sm" href="/appointments">
                  Schedule a visit
                </Link>
              </div>
            </div>
          )}
        </Card>

        {/* Recent results */}
        <Card title="Recent results" icon={<IconLab />}
          action={<Link href="/records">View records</Link>}>
          {results === null ? (
            <Loading label="Loading results…" />
          ) : results.length ? (
            <table className="rb-table">
              <thead>
                <tr>
                  <th>Test</th>
                  <th>Value</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {results.map((r) => (
                  <tr key={r.id}>
                    <td>{r.test || r.kind}</td>
                    <td className="rb-table__num">
                      {r.value ?? "—"}
                      {r.unit ? ` ${r.unit}` : ""}
                    </td>
                    <td><StatusBadge status={r.status || "normal"} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <div className="rb-empty">No recent lab results on file.</div>
          )}
        </Card>
      </div>

      {/* Quick actions */}
      <div>
        <h2 style={{ marginBottom: 14 }}>Quick actions</h2>
        <div className="rb-grid rb-grid--4">
          <Tile href="/intake" icon={<IconIntake />} title="Start Intake"
            sub="Complete new-patient forms" />
          <Tile href="/records" icon={<IconRecords />} title="View Records"
            sub="Encounters & lab results" />
          <Tile href="/roi" icon={<IconRoi />} title="Request Records"
            sub="Release of information" />
          <Tile href="/appointments" icon={<IconCalendar />} title="Schedule"
            sub="Find an open appointment" />
        </div>
      </div>

      <Card title="Your care team" icon={<IconHeart />}>
        <p className="rb-muted" style={{ margin: 0 }}>
          Riverbend Community Health — Primary Care &amp; Specialty Clinics. For urgent
          medical concerns, call 911. For portal help, call (555) 014-2200.
        </p>
      </Card>
    </div>
  );
}

function Tile({
  href,
  icon,
  title,
  sub,
}: {
  href: string;
  icon: React.ReactNode;
  title: string;
  sub: string;
}) {
  return (
    <Link href={href} className="rb-tile">
      <span className="rb-tile__icon">{icon}</span>
      <span className="rb-tile__title">{title}</span>
      <span className="rb-tile__sub">{sub}</span>
    </Link>
  );
}

function Loading({ label }: { label: string }) {
  return (
    <div className="rb-loading">
      <span className="rb-spinner rb-spinner--dark" aria-hidden="true" /> {label}
    </div>
  );
}
