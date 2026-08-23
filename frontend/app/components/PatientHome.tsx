"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import Card from "./Card";
import StatusBadge from "./StatusBadge";
import { IconLab } from "./icons";
import { apiFetch, getUser } from "../lib/session";
import { identityLine, usePatientIdentity } from "../lib/usePatientIdentity";
import { firstName } from "../lib/format";

/**
 * The landing page for a signed-in patient (W9.1, 2026-08-23).
 *
 * The staff dashboard a patient used to land on asked for nothing (no patient
 * id to type) but also showed nothing real — its appointments/results cards
 * were hardcoded empty and its quick actions were staff routes (Intake, ROI)
 * a patient account is refused outright. This renders only what is actually
 * true for the signed-in patient, from self-scoped routes the gateway already
 * derives from the session — never a card whose backend path doesn't exist.
 *
 * Only one card exists so far: results and summaries. Messages (W9.2) and
 * Coverage (W9.3) are added here once their own routes are functional and
 * tested, not before — an inert card is worse than no card.
 */

type SummaryStatus = "approved" | "pending" | "none";

interface AgentSummaryHead {
  available: boolean;
  status: SummaryStatus;
}

const STATUS_LABEL: Record<SummaryStatus, string> = {
  approved: "Approved",
  pending: "Waiting for review",
  none: "Not requested",
};

const STATUS_LOOKUP: Record<SummaryStatus, string> = {
  approved: "confirmed",
  pending: "pending",
  none: "neutral",
};

export default function PatientHome() {
  const user = getUser();
  // Own identity, resolved server-side from the session — the same route
  // /my-results and the agent-summary panel already use. Never a lookup by
  // id: there is no id for this page to ask with.
  const { name: ownName, patientId: ownPatientId } = usePatientIdentity(
    "/api/patient/identity",
    "self"
  );
  const identity = ownPatientId !== null ? identityLine(ownName, ownPatientId) : null;
  const greetingName = ownName ? firstName(ownName) : user?.full_name ? firstName(user.full_name) : "there";

  const [summary, setSummary] = useState<AgentSummaryHead | null>(null);
  const [resultCount, setResultCount] = useState<number | null>(null);
  const [loadError, setLoadError] = useState(false);

  const [requesting, setRequesting] = useState(false);
  const [requestNote, setRequestNote] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoadError(false);
    try {
      const [summaryRes, resultsRes] = await Promise.all([
        apiFetch("/api/patient/agent-summary"),
        apiFetch("/api/patient/summary"),
      ]);
      if (summaryRes.ok) {
        const body = await summaryRes.json();
        setSummary({ available: Boolean(body.available), status: (body.status as SummaryStatus) ?? "none" });
      } else if (summaryRes.status !== 401 && summaryRes.status !== 403) {
        setLoadError(true);
      }
      if (resultsRes.ok) {
        const body = await resultsRes.json();
        setResultCount(Array.isArray(body.items) ? body.items.length : 0);
      }
    } catch {
      setLoadError(true);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function requestSummary() {
    setRequesting(true);
    setRequestNote(null);
    try {
      const res = await apiFetch("/api/patient/agent-summary/request", { method: "POST" });
      if (!res.ok) {
        setRequestNote("We could not request a summary just now. Please try again shortly.");
        return;
      }
      setRequestNote("Requested — your care team will review it before it appears here.");
      await load();
    } catch {
      setRequestNote("We could not reach the server. Please try again.");
    } finally {
      setRequesting(false);
    }
  }

  // One real, useful state, not every state at once. Approved beats pending
  // beats "you have results on file" beats nothing at all — each is only
  // shown when the previous ones do not apply.
  const status = summary?.status ?? "none";
  const primaryText =
    status === "approved"
      ? "Your care team has approved a new summary."
      : status === "pending"
        ? "Your requested summary is waiting for a clinician to review it."
        : resultCount
          ? "You have results on file."
          : "Nothing new to review right now.";

  return (
    <div className="rb-stack">
      <div className="rb-page-head">
        <h1>Good day, {greetingName}</h1>
        {identity && <p className="rb-results-identity">{identity} · Riverbend Community Health</p>}
        <p className="rb-muted" style={{ marginTop: 4 }}>
          Portal messaging is not for emergencies. For urgent medical concerns, call 911.
        </p>
      </div>

      <Card>
        <p style={{ margin: 0, fontSize: "1.05rem" }}>{primaryText}</p>
        <div style={{ marginTop: 12 }}>
          <Link className="rb-btn rb-btn--primary" href="/my-results">
            View your results
          </Link>
        </div>
      </Card>

      <Card title="Results and summaries" icon={<IconLab />}>
        {loadError ? (
          <p role="alert" className="rb-muted">
            We could not load your status just now. Your results are still available on the
            results page.
          </p>
        ) : (
          <>
            <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
              <StatusBadge status={STATUS_LOOKUP[status]} label={STATUS_LABEL[status]} />
              <span className="rb-muted">
                {resultCount === null
                  ? "Checking your results…"
                  : resultCount === 1
                    ? "1 result on file"
                    : `${resultCount} results on file`}
              </span>
            </div>

            <div style={{ display: "flex", gap: 8, marginTop: 14, flexWrap: "wrap" }}>
              <Link className="rb-btn" href="/my-results">
                View results
              </Link>
              <button
                type="button"
                className="rb-btn"
                disabled={requesting || status === "pending"}
                onClick={() => void requestSummary()}
              >
                {requesting ? "Requesting…" : "Request an updated summary"}
              </button>
            </div>

            {requestNote && (
              <p className="rb-muted" role="status" style={{ marginTop: 10 }}>
                {requestNote}
              </p>
            )}
          </>
        )}
      </Card>
    </div>
  );
}
