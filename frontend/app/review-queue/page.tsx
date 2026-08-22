"use client";

import { useCallback, useEffect, useState } from "react";
import AgentDraftPanel from "../components/AgentDraftPanel";
import { apiFetch } from "../lib/session";

/**
 * The clinician review queue.
 *
 * Every case here is content a patient asked for and did not get. Approving
 * releases the record's own words to them; rejecting leaves it withheld for
 * good. Both are real consequences, so this screen is built to make the
 * consequence visible before the click rather than after it:
 *
 *   - the record's full text is shown, never a summary of it — approving
 *     something you have not read is the failure this screen exists to prevent;
 *   - each button says what will happen to the patient, not just what it does
 *     to the row;
 *   - a decision is confirmed, because neither direction can be undone here.
 *
 * The screen enforces nothing. Authorization lives in the gateway and
 * records-service, and a rejected case is invisible to the patient because the
 * read path never puts it in the approved set — not because this page hides a
 * button.
 */

interface ReviewCase {
  id: number;
  patient_id: number;
  record_id: number;
  state: string;
  reason: string | null;
  created_at: string | null;
  record_title: string | null;
  record_kind: string | null;
  record_body: string | null;
  record_date: string | null;
}

type Decision = "approved" | "rejected";

const CONSEQUENCE: Record<Decision, string> = {
  approved: "The patient will see this record's text, exactly as written.",
  rejected: "The patient will continue to see a message telling them to ask their care team.",
};

function CaseCard({
  item,
  busy,
  onDecide,
}: {
  item: ReviewCase;
  busy: boolean;
  onDecide: (id: number, decision: Decision) => void;
}) {
  const [confirming, setConfirming] = useState<Decision | null>(null);

  return (
    <li className="rb-review">
      <div className="rb-review-head">
        <h3 className="rb-review-title">{item.record_title ?? "Record"}</h3>
        <span className="rb-review-meta">
          Patient {item.patient_id} · record #{item.record_id}
          {item.record_date ? ` · ${item.record_date}` : ""}
        </span>
      </div>

      {/* The source text. The clinician decides against this, not a summary. */}
      <blockquote className="rb-review-body">{item.record_body ?? "(no text on this record)"}</blockquote>

      <p className="rb-review-why">
        Withheld because the portal could not quote this safely without interpreting it.
      </p>

      {confirming ? (
        <div className="rb-review-confirm" role="alertdialog" aria-label="Confirm decision">
          <p>
            <strong>{confirming === "approved" ? "Release to patient?" : "Keep withheld?"}</strong>{" "}
            {CONSEQUENCE[confirming]}
          </p>
          <div className="rb-review-actions">
            <button
              type="button"
              className="rb-btn rb-btn--primary"
              disabled={busy}
              onClick={() => onDecide(item.id, confirming)}
            >
              {busy ? "Saving…" : "Yes, continue"}
            </button>
            <button type="button" className="rb-btn" disabled={busy} onClick={() => setConfirming(null)}>
              Cancel
            </button>
          </div>
        </div>
      ) : (
        <div className="rb-review-actions">
          <button type="button" className="rb-btn" disabled={busy} onClick={() => setConfirming("approved")}>
            Release to patient
          </button>
          <button type="button" className="rb-btn" disabled={busy} onClick={() => setConfirming("rejected")}>
            Keep withheld
          </button>
        </div>
      )}
    </li>
  );
}

export default function ReviewQueuePage() {
  const [cases, setCases] = useState<ReviewCase[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<number | null>(null);
  const [done, setDone] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    setCases(null);
    try {
      const res = await apiFetch("/api/review-queue");
      if (res.status === 401 || res.status === 403) {
        setError("You do not have access to the review queue.");
        return;
      }
      if (!res.ok) {
        setError("We could not load the review queue. Please try again shortly.");
        return;
      }
      const body = await res.json();
      setCases(Array.isArray(body.items) ? body.items : []);
    } catch {
      setError("We could not reach the server. Please try again.");
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function decide(id: number, decision: Decision) {
    setBusyId(id);
    setError(null);
    try {
      const res = await apiFetch(`/api/review-queue/${id}/decision`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ decision }),
      });
      if (res.status === 409) {
        // Someone else decided it, or this screen is stale. Reloading is the
        // honest response — silently dropping the row would leave the
        // clinician believing their click landed.
        //
        // The message is set AFTER the reload, not before: load() clears the
        // error banner on entry, so setting it first wiped the only
        // explanation the clinician would get for why their click did nothing.
        await load();
        setError("That case was already decided by someone else. The queue has been refreshed.");
        return;
      }
      if (!res.ok) {
        setError("We could not record that decision. Nothing has changed.");
        return;
      }
      const body = await res.json();
      setDone(
        body.patient_visible
          ? "Released. The patient can now see that record."
          : "Kept withheld. The patient will not see that record."
      );
      setCases((current) => (current ?? []).filter((c) => c.id !== id));
    } catch {
      setError("We could not reach the server. Nothing has changed.");
    } finally {
      setBusyId(null);
    }
  }

  return (
    <main className="rb-review-page">
      <h1>Review queue</h1>
      <p className="rb-review-intro">
        Each case is something a patient asked to see and did not get, because showing it would
        have meant interpreting it. Releasing shows them the record&apos;s own words; keeping it
        withheld leaves them a message asking their care team.
      </p>

      {done ? (
        <p className="rb-review-done" role="status">
          {done}
        </p>
      ) : null}

      {error ? (
        <div className="rb-results-error" role="alert">
          <p>{error}</p>
          <button type="button" onClick={() => void load()}>
            Try again
          </button>
        </div>
      ) : null}

      {cases === null && !error ? <p>Loading the queue…</p> : null}

      {cases && cases.length === 0 ? (
        <p className="rb-review-empty">Nothing is waiting for review.</p>
      ) : null}

      {cases && cases.length > 0 ? (
        <ul className="rb-review-list">
          {cases.map((item) => (
            <CaseCard key={item.id} item={item} busy={busyId === item.id} onDecide={decide} />
          ))}
        </ul>
      ) : null}

      {/* A separate decision on a separate artifact: the queue above releases a
          record's own words, this releases a generated summary. */}
      <AgentDraftPanel />
    </main>
  );
}
